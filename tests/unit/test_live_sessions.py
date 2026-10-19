"""The resident claude-code session (HOT, design §7): many turns, one process.

Driven with the real dialect adapter and authentic stream-json shapes, over a
fake live process — what is under test is the session machinery: lazy spawn
with the first turn's model, stdin-borne turns, result-bounded event slices,
settling without waiting for the process to die, and the restart refusal for
a mid-session model switch.
"""

from __future__ import annotations

import json

import pytest

from cloud_agent_host.drivers import (
    TURN_COMPLETED,
    TURN_FAILED,
    TURN_STARTED,
    ClaudeCodeDriver,
    CodexDriver,
    DriverError,
    HttpAgentLiveSession,
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

_ANSWER = {
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "done"}], "usage": {}},
}
_RESULT = {"type": "result", "subtype": "success"}


class _LiveProcess:
    """A resident process: scripted stdout per turn, recorded stdin."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.feed: list[str] = []
        self.killed = False
        self.stdin_closed = False
        self._exit = 0

    def write_line(self, text: str) -> None:
        self.sent.append(text)

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def lines(self):
        while self.feed:
            yield self.feed.pop(0)

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        return None

    def wait(self) -> int:
        return self._exit

    def stderr_text(self) -> str:
        return ""


def _live(
    process: _LiveProcess, state_dir
) -> tuple[LaunchContext, list[tuple[list[str], dict[str, str]]]]:
    spawned: list[tuple[list[str], dict[str, str]]] = []

    def spawn_live(argv, env):
        spawned.append((argv, env))
        return process

    context = LaunchContext(
        workdir="/w",
        gateway_base_url="http://gw",
        credential="agr",
        provides_isolation=True,
        mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
        state_dir=str(state_dir),
        spawn_live=spawn_live,
    )
    return context, spawned


def _turn_lines(*events: dict) -> list[str]:
    return [json.dumps(event) for event in events]


def test_two_turns_ride_one_process(tmp_path):
    """The point of HOT: the second turn is a stdin line, not a sandbox."""
    process = _LiveProcess()
    context, spawned = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(context)

    process.feed = _turn_lines(_ANSWER, _RESULT)
    first = session.start_turn(TurnInput(prompt="fix it", model="glm-5.1"))
    kinds = [event.event_type for event in first.iter_events()]
    # The result line is both the turn boundary and a dialect event — it flows
    # to the platform exactly as the one-shot world reports it.
    assert kinds == [TURN_STARTED, "message", "lifecycle", TURN_COMPLETED]
    assert first.wait().final_output == "done"

    process.feed = _turn_lines(_ANSWER, _RESULT)
    second = session.start_turn(TurnInput(prompt="now the tests", model="glm-5.1"))
    assert second.wait().status == "completed"

    (argv, _env), *rest = spawned
    assert not rest  # one spawn, two turns
    assert "--input-format" in argv and "stream-json" in argv
    assert argv[argv.index("--model") + 1] == "glm-5.1"
    assert [json.loads(line)["message"]["content"][0]["text"] for line in process.sent] == [
        "fix it",
        "now the tests",
    ]
    assert not process.killed  # settling a turn never costs the residency


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "claude-sonnet-private-deployment"])
def test_live_spawn_binds_subagents_after_the_empty_model_template(tmp_path, model):
    process = _LiveProcess()
    context, spawned = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(context)
    assert spawned == []

    session.ensure_started(model=model, reasoning_effort=None)

    ((argv, env),) = spawned
    assert argv[argv.index("--model") + 1] == model
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == model
    session.close()


@pytest.mark.parametrize("consume_events", [False, True])
@pytest.mark.parametrize("main_answer", [False, True])
def test_claude_child_reply_stays_in_the_stream_without_becoming_the_final_answer(
    tmp_path, consume_events, main_answer
):
    """Delegated findings remain inspectable and never substitute for the main reply."""
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    child = {
        "type": "assistant",
        "parent_tool_use_id": "delegate-call",
        "message": {
            "id": "child-message",
            "content": [{"type": "text", "text": "Child findings"}],
        },
    }
    process.feed = _turn_lines(child, *([_ANSWER] if main_answer else []), _RESULT)
    turn = session.start_turn(TurnInput(prompt="Inspect it", model="m"))
    if consume_events:
        events = list(turn.iter_events())
        child_event = next(
            event for event in events if event.payload.get("parent_tool_use_id") == "delegate-call"
        )
        assert child_event.event_type == "message"
        assert child_event.payload["text"] == "Child findings"
        assert events[-1].event_type == TURN_COMPLETED
        assert "Child findings" not in events[-1].payload["final_output"]
    result = turn.wait()
    assert result.status == "completed"
    assert result.native_turn_settled is True
    assert result.final_output == (
        "done" if main_answer else "(the agent finished without a closing message)"
    )


# Background work as the pinned 2.1.220 reports it on stdout.
_INIT = {"type": "system", "subtype": "init"}
_NOTIFIED = {"type": "result", "subtype": "success", "origin": {"kind": "task-notification"}}


def _say(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _running(*tasks: tuple[str, str]) -> dict:
    return {
        "type": "system",
        "subtype": "background_tasks_changed",
        "tasks": [{"task_id": task_id, "task_type": kind} for task_id, kind in tasks],
    }


def _finished(task_id: str, status: str = "completed") -> dict:
    return {"type": "system", "subtype": "task_notification", "task_id": task_id, "status": status}


def _read(prompt: str) -> dict:
    """The CLI echoing a stdin message at the moment it reads it."""
    return {
        "type": "user",
        "isReplay": True,
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    }


@pytest.mark.parametrize("finished_before_the_answer", [False, True])
def test_claude_turn_ends_once_its_background_agent_is_answered(
    tmp_path, finished_before_the_answer
):
    """As the one-shot CLI holds its result, the turn waits for its background agent.

    The answer to the finished agent comes in a turn Claude opens itself after
    the prompt's result; read by the next turn, it would be taken as its answer.
    """
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    finished = [_running(), _finished("agent-1")]
    process.feed = _turn_lines(
        _running(("agent-1", "local_agent")),
        *(finished if finished_before_the_answer else []),
        _say("launched"),
        _RESULT,
        *([] if finished_before_the_answer else finished),
        _INIT,
        _say("the agent found it"),
        _NOTIFIED,
    )
    first = session.start_turn(TurnInput(prompt="look into it", model="m"))

    result = first.wait()
    assert result.status == "completed"
    assert result.final_output == "launched\n\nthe agent found it"
    assert process.feed == []

    process.feed = _turn_lines(_say("next answer"), _RESULT)
    assert session.start_turn(TurnInput(prompt="next", model="m")).wait().final_output == (
        "next answer"
    )


def test_claude_turn_waits_for_every_background_agent(tmp_path):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(
        _running(("a", "local_agent"), ("b", "local_agent")),
        _say("launched two"),
        _RESULT,
        _running(("b", "local_agent")),
        _finished("a"),
        _INIT,
        _say("a is done"),
        _NOTIFIED,
        _running(),
        _finished("b"),
        _INIT,
        _say("b is done"),
        _NOTIFIED,
    )
    result = session.start_turn(TurnInput(prompt="split it", model="m")).wait()

    assert result.final_output == "launched two\n\na is done\n\nb is done"
    assert process.feed == []


def test_claude_turn_does_not_wait_for_a_background_shell_or_take_its_answer_as_the_next(
    tmp_path,
):
    """A shell may never end, so the turn does not wait for it.

    It finishing between turns still opens a notification turn. The next turn
    reads that turn but ends only with the answer to its own prompt.
    """
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_running(("shell-1", "local_bash")), _say("server up"), _RESULT)
    assert session.start_turn(TurnInput(prompt="serve", model="m")).wait().final_output == (
        "server up"
    )

    process.feed = _turn_lines(
        _running(),
        _finished("shell-1"),
        _INIT,
        _say("the server exited"),
        _NOTIFIED,
        _read("is it up?"),
        _INIT,
        _say("no, it exited"),
        _RESULT,
    )
    second = session.start_turn(TurnInput(prompt="is it up?", model="m"))

    assert second.wait().final_output == "the server exited\n\nno, it exited"
    assert process.feed == []


def test_claude_turn_ends_with_a_notification_turn_that_took_its_prompt(tmp_path):
    """Read while Claude answers a notification, the prompt is answered in that turn."""
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_running(("shell-1", "local_bash")), _RESULT)
    session.start_turn(TurnInput(prompt="serve", model="m")).wait()

    process.feed = _turn_lines(
        _running(),
        _finished("shell-1"),
        _INIT,
        _read("is it up?"),
        _say("it exited just now"),
        _NOTIFIED,
    )
    second = session.start_turn(TurnInput(prompt="is it up?", model="m"))

    assert second.wait().final_output == "it exited just now"
    assert process.feed == []


@pytest.mark.parametrize("stopped", [True, False])
def test_claude_cancel_while_waiting_stops_the_background_agents(tmp_path, stopped):
    """The interrupt stops background agents and opens no turn; its reply ends this one.

    Should an agent still be listed, the process retires so it ends with it.
    """
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    killed = [_running(), _finished("agent-1", "stopped")] if stopped else []
    process.feed = _turn_lines(
        _running(("agent-1", "local_agent")),
        _say("launched"),
        _RESULT,
        *killed,
        {"type": "control_response", "response": {"subtype": "success"}},
    )
    turn = session.start_turn(TurnInput(prompt="look into it", model="m"))
    for event in turn.iter_events():
        if event.payload.get("phase") == "result":
            turn.cancel()

    assert turn.wait().status == "cancelled"
    assert json.loads(process.sent[-1])["request"]["subtype"] == "interrupt"
    assert process.feed == []
    assert session.retire_after_turn is not stopped


def test_claude_message_sent_while_waiting_goes_as_the_next_turn(tmp_path):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(
        _running(("agent-1", "local_agent")),
        _RESULT,
        _running(),
        _finished("agent-1"),
        _INIT,
        _NOTIFIED,
    )
    turn = session.start_turn(TurnInput(prompt="look into it", model="m"))
    outcomes = []
    for event in turn.iter_events():
        if event.payload.get("phase") == "result" and not outcomes:
            outcomes.append(turn.inject("and the tests too", message_id="m-1"))

    assert [outcome.accepted for outcome in outcomes] == [False]
    assert turn.wait().status == "completed"
    assert all("and the tests too" not in line for line in process.sent)


def test_a_model_switch_mid_session_is_a_restart_not_a_lie(tmp_path):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="a")).wait()

    with pytest.raises(DriverError, match="restart"):
        session.start_turn(TurnInput(prompt="p", model="b"))


def test_a_process_dying_mid_turn_settles_as_failed(tmp_path):
    """The stream ending without a result event is a death, not a success."""
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_ANSWER)  # no result: the process died
    process._exit = 0  # even a clean exit code must not read as a settled turn

    turn = session.start_turn(TurnInput(prompt="p", model="m"))
    list(turn.iter_events())

    assert turn.wait().status == "failed"
    assert turn.wait().native_turn_settled is False


@pytest.mark.parametrize("consume_events", [False, True])
@pytest.mark.parametrize(
    "failure_fields",
    [
        {"is_error": True},
        {"is_error": False, "terminal_reason": "api_error", "api_error_status": 503},
        {"terminal_reason": "api_error", "api_error_status": 503},
    ],
)
def test_claude_error_result_fails_the_turn_without_poisoning_next_turn(
    tmp_path, consume_events, failure_fields
):
    process = _LiveProcess()
    context, spawned = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(context)
    process.feed = _turn_lines(
        _ANSWER,
        {"type": "result", "subtype": "success", "result": "API Error: 503", **failure_fields},
    )
    failed = session.start_turn(TurnInput(prompt="hi", model="deepseek-v4-flash"))
    if consume_events:
        kinds = [event.event_type for event in failed.iter_events()]
        assert "error" in kinds
        assert kinds[-1] == TURN_FAILED
        assert TURN_COMPLETED not in kinds
    assert failed.wait().status == "failed"
    assert failed.wait().exit_code != 0
    assert failed.wait().native_turn_settled is True
    assert failed.wait().final_output == ""

    process.feed = _turn_lines(_ANSWER, _RESULT)
    retried = session.start_turn(TurnInput(prompt="hi", model="deepseek-v4-flash"))
    assert retried.wait().status == "completed"
    assert len(spawned) == 1 and not process.killed


def test_close_ends_the_residency(tmp_path, monkeypatch):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="m")).wait()

    clock = iter([0.0, 3.0])
    monkeypatch.setattr("cloud_agent_host.drivers.time.monotonic", lambda: next(clock))
    session.close()

    assert process.stdin_closed and process.killed
    with pytest.raises(DriverError, match="closed"):
        session.start_turn(TurnInput(prompt="again", model="m"))


def test_settled_claude_close_allows_pending_native_history_to_flush(tmp_path, monkeypatch):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_ANSWER, _RESULT)
    session.start_turn(TurnInput(prompt="p", model="m")).wait()
    flushed = False

    def flush(_):
        nonlocal flushed
        assert process.stdin_closed
        assert not process.killed
        flushed = True

    monkeypatch.setattr(process, "poll", lambda: 0 if flushed else None)
    monkeypatch.setattr("cloud_agent_host.drivers.time.sleep", flush)
    session.close()
    session.close()

    assert flushed
    assert not process.killed


@pytest.mark.parametrize("consume_events", [False, True])
@pytest.mark.parametrize("settled", [False, True])
def test_api_retry_rotates_only_after_native_result(tmp_path, monkeypatch, consume_events, settled):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    retry = {"type": "system", "subtype": "api_retry", "attempt": 1}
    # Pre-observe a notification: later notices still require rotation
    # independently of their normalized progress rows.
    session.runtime.parse_event(json.dumps(retry))
    process.feed = _turn_lines(retry, _ANSWER, *([_RESULT] if settled else []))
    monkeypatch.setattr(process, "poll", lambda: 0 if process.stdin_closed else None)
    turn = session.start_turn(TurnInput(prompt="hi", model="deepseek-v4-flash"))
    if consume_events:
        list(turn.iter_events())
        assert not session.closed
    result = turn.wait()
    assert result.native_turn_settled is settled
    assert session.retire_after_turn is settled
    assert not session.closed
    assert not process.stdin_closed
    assert not process.killed
    assert result.status == ("completed" if settled else "failed")
    if settled:
        with pytest.raises(DriverError, match="native session restart"):
            session.start_turn(TurnInput(prompt="next", model="deepseek-v4-flash"))


def test_launch_live_without_the_live_spawn_refuses(tmp_path):
    context, _ = _live(_LiveProcess(), tmp_path)
    object.__setattr__(context, "spawn_live", None)
    with pytest.raises(DriverError, match="live-input spawn"):
        ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(context)


def test_process_alive_tracks_spawn_death_and_close(tmp_path):
    """What the platform labels ``hot`` hangs off this: unspawned and closed
    sessions are not alive, and a process that died idle stops counting."""
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    assert session.process_alive() is False  # lazy spawn: nothing yet

    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="m")).wait()
    assert session.process_alive() is True

    process.poll = lambda: 1  # died while idle
    assert session.process_alive() is False

    process.poll = lambda: None
    session.close()
    assert session.process_alive() is False


def test_launch_resident_live_resumes_the_threads_session(tmp_path):
    """The HOT mirror of launch_resident: same on-disk resume, live process."""
    process = _LiveProcess()
    context, spawned = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_resident_live(
        context, session_id="sess-prev"
    )

    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="m")).wait()

    (argv, _env), *rest = spawned
    assert not rest
    assert argv[argv.index("--resume") + 1] == "sess-prev"


def test_launch_resident_live_needs_the_state_mount(tmp_path):
    context, _ = _live(_LiveProcess(), tmp_path)
    object.__setattr__(context, "state_dir", None)
    with pytest.raises(DriverError, match="native session state"):
        ClaudeCodeDriver(ClaudeCodeRuntime()).launch_resident_live(context, session_id="s")


@pytest.mark.parametrize("kind", ["init", "assistant", "result"])
def test_claude_rejects_a_different_root_session_before_accepting_its_output(tmp_path, kind):
    process = _LiveProcess()
    context, _ = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_resident_live(
        context, session_id="retained-session"
    )
    record = (
        {"type": "system", "subtype": "init"}
        if kind == "init"
        else dict(_ANSWER if kind == "assistant" else _RESULT)
    )
    process.feed = _turn_lines(dict(record, session_id="unexpected-new-session"))
    turn = session.start_turn(TurnInput(prompt="Continue our task", model="deepseek-v4-flash"))
    with pytest.raises(DriverError, match="different native session"):
        list(turn.iter_events())
    assert session.runtime.session_id != "unexpected-new-session"


def test_claude_does_not_treat_a_child_agent_session_as_a_root_reset(tmp_path):
    process = _LiveProcess()
    context, _ = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_resident_live(context, session_id="root")
    process.feed = _turn_lines(
        {"type": "system", "subtype": "init", "session_id": "root"},
        {
            "type": "system",
            "subtype": "init",
            "session_id": "child",
            "parent_tool_use_id": "tool-child",
        },
        dict(_ANSWER, session_id="child", parent_tool_use_id="tool-child"),
        dict(_RESULT, session_id="root"),
    )
    assert (
        session.start_turn(TurnInput(prompt="p", model="deepseek-v4-flash")).wait().status
        == "completed"
    )
    assert session.runtime.session_id == "root"


def test_the_first_turn_fixes_the_sessions_effort(tmp_path):
    """Effort is a launch flag, so the resident process carries the first one."""
    process = _LiveProcess()
    context, spawned = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(context)

    process.feed = _turn_lines(_RESULT)
    session.start_turn(
        TurnInput(prompt="think hard", model="glm-5.1", reasoning_effort="max")
    ).wait()

    (argv, _env), *rest = spawned
    assert not rest
    assert argv[argv.index("--effort") + 1] == "max"
    # Beside the model, where the one-shot invocation puts it too.
    assert argv.index("--effort") == argv.index("--model") + 2


def test_an_effort_switch_mid_session_is_a_restart_not_a_lie(tmp_path):
    """Silently serving the old depth would be indistinguishable from success."""
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="m", reasoning_effort="low")).wait()

    with pytest.raises(DriverError, match="restart"):
        session.start_turn(TurnInput(prompt="p", model="m", reasoning_effort="high"))


def test_dropping_back_to_auto_mid_session_is_also_a_restart(tmp_path):
    """Auto is a depth like any other, not "no opinion" the process can absorb."""
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="m", reasoning_effort="high")).wait()

    with pytest.raises(DriverError, match="restart"):
        session.start_turn(TurnInput(prompt="p", model="m"))


def test_an_auto_session_spawns_without_the_flag(tmp_path):
    """Auto adds nothing to the argv — today's behaviour, bit for bit."""
    process = _LiveProcess()
    context, spawned = _live(process, tmp_path)
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(context)

    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="m")).wait()

    (argv, _env) = spawned[0]
    assert "--effort" not in argv


def test_claude_interrupt_is_native_and_keeps_the_process(tmp_path):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_RESULT)
    turn = session.start_turn(TurnInput(prompt="stop", model="m"))

    turn.cancel()
    assert json.loads(process.sent[-1])["request"]["subtype"] == "interrupt"
    assert turn.wait().status == "cancelled"
    assert not process.killed
    assert session.process_alive()


def _protocol_context(process: _LiveProcess, tmp_path):
    """The same live seam without assumptions about a particular CLI."""
    return _live(process, tmp_path)


def test_codex_app_server_two_turns_and_prompt_free_fork(tmp_path):
    process = _LiveProcess()
    context, spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(context)
    session.ensure_started(model="gpt", reasoning_effort="high")

    process.feed = _turn_lines(
        {"id": "turn-3", "result": {"turn": {"id": "turn-a"}}},
    )
    first = session.start_turn(TurnInput(prompt="one", model="gpt", reasoning_effort="high"))
    process.feed.extend(
        _turn_lines(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "id": "i", "text": "first"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-a", "status": "completed"}},
            },
        )
    )
    assert first.wait().final_output == "first"

    process.feed = _turn_lines(
        {"id": "turn-4", "result": {"turn": {"id": "turn-b"}}},
    )
    second = session.start_turn(TurnInput(prompt="two", model="gpt", reasoning_effort="high"))
    process.feed.extend(
        _turn_lines(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "id": "j", "text": "second"}},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-b", "status": "completed"}},
            },
        )
    )
    assert second.wait().final_output == "second"

    process.feed = _turn_lines(
        {
            "id": "fork-read-op-5",
            "result": {"thread": {"id": "thread-a", "turns": [{"id": "turn-a"}, {"id": "turn-b"}]}},
        },
        {"id": "fork-op-6", "result": {"thread": {"id": "thread-child"}}},
    )
    assert (
        session.fork_native(
            source_session_id="thread-a",
            operation_id="op",
            source_native_turn_no=1,
        )
        == "thread-child"
    )
    assert len(spawned) == 1
    argv, env = spawned[0]
    assert argv[-1] == "app-server"
    assert env["CODEX_HOME"] == "/agent-state"
    sent = [json.loads(line) for line in process.sent]
    assert [line.get("method") for line in sent if line.get("method") == "turn/start"] == [
        "turn/start",
        "turn/start",
    ]
    fork = next(line for line in sent if line.get("method") == "thread/fork")
    assert fork["params"]["threadId"] == "thread-a"
    assert fork["params"]["lastTurnId"] == "turn-a"
    assert fork["params"]["excludeTurns"] is True
    assert all("one" not in json.dumps(line) for line in sent if line is fork)


def test_codex_mutable_tool_updates_emit_one_tool_use(tmp_path):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(context)
    session.ensure_started(model="gpt", reasoning_effort=None)
    process.feed = _turn_lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    turn = session.start_turn(TurnInput(prompt="run it", model="gpt"))
    tool_item = {
        "type": "commandExecution",
        "id": "tool-a",
        "command": "pytest",
    }
    process.feed = _turn_lines(
        {"method": "item/started", "params": {"item": tool_item}},
        {"method": "item/updated", "params": {"item": tool_item}},
        {
            "method": "item/completed",
            "params": {"item": {**tool_item, "exitCode": 0, "aggregatedOutput": "ok"}},
        },
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-a", "status": "completed"}},
        },
    )

    kinds = [event.event_type for event in turn.iter_events()]

    assert kinds.count("tool_use") == 1
    assert kinds.count("tool_result") == 1


def test_codex_subagent_thread_events_belong_to_the_call_that_spawned_it(tmp_path):
    """The pinned 0.145.0 app-server sends a subagent thread's notifications too.

    The order is the one it produced for one spawn: the spawn completes naming
    the child thread before the child's own items arrive.
    """
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(context)
    session.ensure_started(model="gpt", reasoning_effort=None)
    process.feed = _turn_lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    turn = session.start_turn(TurnInput(prompt="delegate", model="gpt"))

    def on(thread, method, item=None, **params):
        return {
            "method": method,
            "params": {"threadId": thread, **({"item": item} if item else {}), **params},
        }

    spawn = {"type": "collabAgentToolCall", "id": "call-spawn", "tool": "spawnAgent"}
    command = {"type": "commandExecution", "id": "child-cmd", "command": "ls"}
    process.feed = _turn_lines(
        on("thread-a", "item/started", {**spawn, "receiverThreadIds": []}),
        on("child-t", "thread/status/changed", status={"type": "active"}),
        on("thread-a", "item/completed", {**spawn, "receiverThreadIds": ["child-t"]}),
        on("child-t", "item/agentMessage/delta", itemId="child-msg", delta="CHILD-"),
        on(
            "child-t",
            "item/completed",
            {"type": "agentMessage", "id": "child-msg", "text": "CHILD"},
        ),
        on("child-t", "item/completed", {"type": "reasoning", "id": "child-r", "summary": ["hm"]}),
        on("child-t", "item/started", command),
        on("child-t", "item/completed", {**command, "exitCode": 0, "aggregatedOutput": "a"}),
        on("child-t", "error", error={"message": "the child's request failed"}),
        on("child-t", "turn/completed", turn={"id": "turn-child", "status": "completed"}),
        on("thread-a", "item/completed", {"type": "agentMessage", "id": "main", "text": "DONE"}),
        on("thread-a", "turn/completed", turn={"id": "turn-a", "status": "completed"}),
    )
    events = [event for event in turn.iter_events() if not event.is_floor]
    owner = {"agent_thread_id": "child-t", "parent_tool_use_id": "call-spawn"}

    def owned(event):
        return {key: event.payload[key] for key in owner if key in event.payload}

    assert turn.wait().final_output == "DONE"
    messages = [event for event in events if event.event_type == "message"]
    assert [(event.payload["text"], owned(event)) for event in messages] == [
        ("CHILD", owner),
        ("DONE", {}),
    ]
    assert "preview_id" not in messages[0].payload
    assert not any(event.payload.get("protocol") == "reply-preview" for event in events)
    for kind in ("thinking", "tool_use", "tool_result"):
        assert [owned(event) for event in events if event.event_type == kind] == [owner]
    assert [event.event_type for event in events].count("error") == 0
    results = [event for event in events if event.payload.get("phase") == "result"]
    assert [owned(event) for event in results] == [owner, {}]
    # Before the spawn named the thread, only the thread itself is known.
    early = next(
        event
        for event in events
        if event.event_type == "raw"
        and event.payload["notification"]["method"] == "thread/status/changed"
    )
    assert owned(early) == {"agent_thread_id": "child-t"}


def test_pi_rpc_two_turns_and_native_abort(tmp_path):
    process = _LiveProcess()
    context, spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {
            "id": "state-1",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "pi-a", "sessionFile": "/agent-state/pi-a.jsonl"},
        }
    )
    session = PiDriver(PiRuntime()).launch_live(context)
    session.ensure_started(model="m", reasoning_effort=None)

    turn = session.start_turn(TurnInput(prompt="hello", model="m"))
    prompt_id = json.loads(process.sent[-1])["id"]
    process.feed = _turn_lines(
        {"id": prompt_id, "type": "response", "command": "prompt", "success": True},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "content": "pi says hi"},
        },
        {"type": "agent_end", "messages": []},
    )
    assert turn.wait().final_output == "pi says hi"

    process.feed = _turn_lines()
    interrupted = session.start_turn(TurnInput(prompt="long", model="m"))
    interrupted.cancel()
    assert json.loads(process.sent[-1])["type"] == "abort"
    process.feed = _turn_lines({"type": "agent_end", "messages": []})
    assert interrupted.wait().status == "cancelled"
    assert not process.killed
    argv, _env = spawned[0]
    assert argv[argv.index("--mode") + 1] == "rpc"
    assert "-p" not in argv


def _codex_turn(tmp_path):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(context)
    session.ensure_started(model="gpt", reasoning_effort=None)
    process.feed = _turn_lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    return process, session.start_turn(TurnInput(prompt="read", model="gpt"))


def test_codex_reports_the_error_that_fails_its_turn(tmp_path):
    """Staging's 2026-09-27 turn: five retried 403s, then the final one."""
    process, turn = _codex_turn(tmp_path)
    refused = (
        "unexpected status 403 Forbidden: This account has no active API key, so its "
        "spending limit cannot be applied."
    )
    retried = {
        "message": "Reconnecting... 1/5",
        "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": 403}},
        "additionalDetails": f"{refused}, url: http://agent-gateway:8080/v1/responses",
    }
    final = {
        "message": f"{refused}, url: http://agent-gateway:8080/v1/responses, request id: r-1",
        "codexErrorInfo": "other",
        "additionalDetails": None,
    }
    process.feed = _turn_lines(
        {"method": "configWarning", "params": {"summary": "Codex's Linux sandbox uses bubblewrap"}},
        {"method": "error", "params": {"error": retried, "willRetry": True, "turnId": "turn-a"}},
        {"method": "error", "params": {"error": final, "willRetry": False, "turnId": "turn-a"}},
        {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-a", "status": "failed", "error": final}},
        },
    )
    events = list(turn.iter_events())

    errors = [event.payload for event in events if event.event_type == "error"]
    # A retried error is not the turn's; the gateway relay address is dropped
    # from the text and kept, with the rest, in the error Codex sent.
    assert errors == [{"text": refused, "error": final}]
    assert turn.wait().status == "failed"


def test_codex_records_the_server_an_mcp_call_went_to(tmp_path):
    """Codex names an MCP call by the tool's own name, which may be "task".

    The server it went to is what tells the trajectory it is not Codex's own
    delegation, so the call records it. A command names no server.
    """
    process, turn = _codex_turn(tmp_path)
    arguments = {"description": "Review", "prompt": "Review this change."}
    mcp = {
        "type": "mcpToolCall",
        "id": "mcp-1",
        "server": "session",
        "tool": "review__task",
        "status": "inProgress",
        "arguments": arguments,
    }
    command = {"type": "commandExecution", "id": "cmd-1", "command": "ls"}
    process.feed = _turn_lines(
        {"method": "item/started", "params": {"item": mcp}},
        {"method": "item/started", "params": {"item": command}},
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}},
    )
    calls = [event.payload for event in turn.iter_events() if event.event_type == "tool_use"]

    assert calls == [
        {"name": "review__task", "id": "mcp-1", "input": arguments, "mcp_server": "session"},
        {"name": "commandExecution", "id": "cmd-1", "input": "ls"},
    ]


def test_codex_reports_what_an_mcp_call_returned_as_readable_text(tmp_path):
    """An MCP call's item carries its result as an object, and its error as one.

    A command's output is text already; the result's blocks and the error's
    message are what the call returned, not a Python rendering of them.
    """
    process, turn = _codex_turn(tmp_path)
    returned = {
        "content": [{"type": "text", "text": "Issue #7: the build fails"}],
        "structuredContent": None,
    }

    def call(call_id: str, status: str, **outcome: object) -> dict:
        item = {
            "type": "mcpToolCall",
            "id": call_id,
            "server": "session",
            "tool": "github__get_issue",
            "status": status,
            "arguments": {"number": 7},
            **outcome,
        }
        return {"method": "item/completed", "params": {"item": item}}

    process.feed = _turn_lines(
        call("mcp-1", "completed", result=returned, error=None),
        call("mcp-2", "failed", result=None, error={"message": "issue 8 does not exist"}),
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}},
    )
    results = [event.payload for event in turn.iter_events() if event.event_type == "tool_result"]

    assert [result["tool_use_id"] for result in results] == ["mcp-1", "mcp-2"]
    assert json.loads(results[0]["content"]) == returned
    assert results[0]["is_error"] is False
    assert results[1]["content"] == "issue 8 does not exist"
    assert results[1]["is_error"] is True


def test_a_codex_json_rpc_error_reads_as_its_message(tmp_path):
    process, turn = _codex_turn(tmp_path)
    error = {"code": -32600, "message": "turn is not active"}
    process.feed = _turn_lines(
        {"id": "late-1", "error": error},
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "failed"}}},
    )
    events = list(turn.iter_events())

    assert [e.payload for e in events if e.event_type == "error"] == [
        {"text": "turn is not active", "error": error}
    ]


def test_pi_reports_a_prompt_it_refused_before_running(tmp_path):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {
            "id": "state-1",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "pi-a", "sessionFile": "/agent-state/pi-a.jsonl"},
        }
    )
    session = PiDriver(PiRuntime()).launch_live(context)
    session.ensure_started(model="m", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="hello", model="m"))
    prompt_id = json.loads(process.sent[-1])["id"]
    # pi 0.73.1 answers a prompt this way only when it fails before preflight.
    process.feed = _turn_lines(
        {
            "id": prompt_id,
            "type": "response",
            "command": "prompt",
            "success": False,
            "error": "No API key found for freeinference",
        }
    )
    events = list(turn.iter_events())

    assert [e.payload for e in events if e.event_type == "error"] == [
        {"text": "No API key found for freeinference"}
    ]
    assert turn.wait().status == "failed"


def _pi_turn(tmp_path):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {
            "id": "state-1",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "pi-a", "sessionFile": "/agent-state/pi-a.jsonl"},
        }
    )
    session = PiDriver(PiRuntime()).launch_live(context)
    session.ensure_started(model="m", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="hello", model="m"))
    prompt_id = json.loads(process.sent[-1])["id"]
    return process, turn, prompt_id


def test_pi_rpc_turn_outlasts_a_run_pi_retries(tmp_path):
    """pi 1.2.0 on two 500s: the turn is the run that answered, not the first.

    Since 0.75.4 a transient failure ends its run with ``willRetry`` before
    pi runs it again. Settling there failed a turn pi went on to answer, and
    left that answer to land in whatever turn came next.
    """
    process, turn, prompt_id = _pi_turn(tmp_path)
    failed = {"role": "assistant", "stopReason": "error", "errorMessage": "500: transient"}
    process.feed = _turn_lines(
        {"id": prompt_id, "type": "response", "command": "prompt", "success": True},
        {"type": "agent_end", "messages": [failed], "willRetry": True},
        {
            "type": "auto_retry_start",
            "attempt": 1,
            "maxAttempts": 3,
            "delayMs": 2000,
            "errorMessage": "500: transient",
        },
        {"type": "agent_start"},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "contentIndex": 0, "content": "after"},
        },
        {
            "type": "message_end",
            "message": {"role": "assistant", "responseId": "chatcmpl-3", "usage": {"input": 1}},
        },
        {"type": "auto_retry_end", "attempt": 1, "success": True},
        {
            "type": "agent_end",
            "messages": [{"role": "assistant", "stopReason": "stop"}],
            "willRetry": False,
        },
    )
    events = list(turn.iter_events())

    assert not [event for event in events if event.event_type == "error"]
    [message] = [event for event in events if event.event_type == "message"]
    # Held for the response id its message_end names, then released there.
    assert message.payload["usage_id"] == "chatcmpl-3"
    result = turn.wait()
    assert (result.status, result.final_output) == ("completed", "after")


def test_pi_rpc_turn_fails_with_the_run_pi_stops_retrying(tmp_path):
    process, turn, prompt_id = _pi_turn(tmp_path)
    failed = {"role": "assistant", "stopReason": "error", "errorMessage": "500: always"}
    process.feed = _turn_lines(
        {"id": prompt_id, "type": "response", "command": "prompt", "success": True},
        {"type": "agent_end", "messages": [failed], "willRetry": True},
        {"type": "agent_end", "messages": [failed], "willRetry": False},
    )
    events = list(turn.iter_events())

    assert [event.payload for event in events if event.event_type == "error"] == [
        {"text": "500: always"}
    ]
    assert turn.wait().status == "failed"


def test_pi_records_the_server_an_mcp_call_went_to(tmp_path):
    """pi's bridge registers a relay tool as ``session__<alias>__<tool>``.

    The relay's tool may be called "task", so the call names the server, as a
    Codex call does, and the trajectory does not read it as a delegation. A
    tool of pi's own names none.
    """
    process, turn, prompt_id = _pi_turn(tmp_path)
    arguments = {"description": "Review", "prompt": "Review this change."}

    def call(call_id: str, name: str, arguments: dict) -> dict:
        return {
            "type": "message_update",
            "message": {"role": "assistant", "content": []},
            "assistantMessageEvent": {
                "type": "toolcall_end",
                "contentIndex": 0,
                "toolCall": {
                    "type": "toolCall",
                    "id": call_id,
                    "name": name,
                    "arguments": arguments,
                },
            },
        }

    process.feed = _turn_lines(
        {"id": prompt_id, "type": "response", "command": "prompt", "success": True},
        call("mcp-1", "session__review__task", arguments),
        call("bash-1", "bash", {"command": "ls"}),
        {"type": "agent_end", "messages": []},
    )
    calls = [event.payload for event in turn.iter_events() if event.event_type == "tool_use"]

    assert calls == [
        {
            "name": "session__review__task",
            "id": "mcp-1",
            "input": arguments,
            "mcp_server": "session",
        },
        {"name": "bash", "id": "bash-1", "input": {"command": "ls"}},
    ]


@pytest.mark.parametrize(
    ("driver", "runtime"), [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
def test_http_runtimes_record_the_server_an_mcp_call_went_to(tmp_path, driver, runtime):
    """OpenCode and Kilo name a relay tool ``session_<alias>__<tool>``.

    Their own ``task`` is a delegation and names no server; the relay's tool of
    the same name is not, so it names the server it went to.
    """
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "ses-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="m", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="hello", model="m"))
    turn_id = json.loads(process.sent[-1])["id"]
    arguments = {"description": "Review", "prompt": "Review this change."}

    def tool(call_id: str, name: str) -> dict:
        part = {
            "id": f"part-{call_id}",
            "callID": call_id,
            "type": "tool",
            "tool": name,
            "state": {"status": "running", "input": arguments},
        }
        return {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {"type": "message.part.updated", "properties": {"part": part}},
        }

    process.feed = _turn_lines(
        tool("mcp-1", "session_review__task"),
        tool("task-1", "task"),
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )
    calls = [event.payload for event in turn.iter_events() if event.event_type == "tool_use"]

    assert calls == [
        {
            "name": "session_review__task",
            "id": "mcp-1",
            "input": arguments,
            "mcp_server": "session",
        },
        {"name": "task", "id": "task-1", "input": arguments},
    ]


def test_pi_rpc_forks_before_the_next_native_prompt(tmp_path):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {
            "id": "state-1",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "pi-a"},
        }
    )
    session = PiDriver(PiRuntime()).launch_live(context)
    session.ensure_started(model="m", reasoning_effort=None)
    process.feed = _turn_lines(
        {
            "id": "fork-list-op-2",
            "type": "response",
            "command": "get_fork_messages",
            "success": True,
            "data": {
                "messages": [
                    {"entryId": "user-one", "text": "one"},
                    {"entryId": "user-two", "text": "two"},
                ]
            },
        },
        {
            "id": "fork-op-3",
            "type": "response",
            "command": "fork",
            "success": True,
            "data": {"text": "two", "cancelled": False},
        },
        {
            "id": "fork-state-op-4",
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "pi-child"},
        },
    )

    assert (
        session.fork_native(
            source_session_id="pi-a",
            operation_id="op",
            source_native_turn_no=1,
        )
        == "pi-child"
    )
    sent = [json.loads(line) for line in process.sent]
    assert sent[-2] == {"id": "fork-op-3", "type": "fork", "entryId": "user-two"}


@pytest.mark.parametrize(
    ("driver", "runtime", "runtime_name"),
    [
        (OpenCodeDriver, OpencodeRuntime, "opencode"),
        (KiloDriver, KiloRuntime, "kilo"),
    ],
)
def test_http_server_bridge_two_turns_and_native_fork(tmp_path, driver, runtime, runtime_name):
    process = _LiveProcess()
    context, spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "runtime": runtime_name, "session_id": "ses-a"})
    session = driver(runtime()).launch_live(context)
    assert isinstance(session, HttpAgentLiveSession)
    session.ensure_started(model="m", reasoning_effort="high")

    turn = session.start_turn(TurnInput(prompt="hello", model="m", reasoning_effort="high"))
    turn_id = json.loads(process.sent[-1])["id"]
    process.feed = _turn_lines(
        {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {
                "type": "message.updated",
                "properties": {
                    "info": {"id": "assistant-1", "sessionID": "ses-a", "role": "assistant"}
                },
            },
        },
        {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "id": "part-1",
                        "messageID": "assistant-1",
                        "sessionID": "ses-a",
                        "type": "text",
                        "text": "server answer",
                    }
                },
            },
        },
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )
    assert turn.wait().final_output == "server answer"

    process.feed = _turn_lines(
        {
            "bridge": "fork_result",
            "id": "fork-op-2",
            "source_session_id": "ses-a",
            "session_id": "ses-child",
        }
    )
    assert (
        session.fork_native(
            source_session_id="ses-a",
            operation_id="op",
            source_native_turn_no=1,
        )
        == "ses-child"
    )
    assert len(spawned) == 1
    argv, env = spawned[0]
    assert argv == ["http-agent-live-bridge", runtime_name]
    assert env["AGENT_LIVE_WORKDIR"] == "/w"
    assert json.loads(process.sent[-1])["source_native_turn_no"] == 1


def test_every_bundled_driver_only_advertises_shared_live_after_wiring():
    for driver, runtime in (
        (ClaudeCodeDriver, ClaudeCodeRuntime),
        (CodexDriver, CodexRuntime),
        (PiDriver, PiRuntime),
        (OpenCodeDriver, OpencodeRuntime),
        (KiloDriver, KiloRuntime),
    ):
        capabilities = driver(runtime()).capabilities()
        assert capabilities.resident_transport == "supervisor_live"
        assert capabilities.concurrent_shared_workspace is True
        assert capabilities.fork == "at_anchor"


def test_codex_previews_its_answer_in_batches_under_the_item_id(tmp_path):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(context)
    session.ensure_started(model="gpt", reasoning_effort=None)
    process.feed = _turn_lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    turn = session.start_turn(TurnInput(prompt="read", model="gpt"))
    words = ["Hel", "lo ", "the", "re."]
    process.feed = _turn_lines(
        *(
            {"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": word}}
            for word in words
        ),
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": "m", "text": "Hello there."}},
        },
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}},
    )
    events = list(turn.iter_events())
    pieces = [event.payload for event in events if event.payload.get("protocol") == "reply-preview"]
    # The first piece goes out at once; later ones are gathered, so a burst of
    # deltas is fewer stored rows, and whatever was shown is the answer's start.
    assert pieces[0] == {"protocol": "reply-preview", "preview_id": "m", "delta": "Hel"}
    assert len(pieces) < len(words)
    assert "Hello there.".startswith("".join(piece["delta"] for piece in pieces))
    answer = next(event for event in events if event.event_type == "message")
    assert answer.payload["preview_id"] == "m"


def test_codex_trajectory_keeps_native_identity_and_structured_streams(tmp_path):
    process = _LiveProcess()
    context, _spawned = _protocol_context(process, tmp_path)
    process.feed = _turn_lines(
        {"id": "initialize-1", "result": {"userAgent": "codex"}},
        {"id": "thread-2", "result": {"thread": {"id": "thread-a"}}},
    )
    session = CodexDriver(CodexRuntime()).launch_live(context)
    session.ensure_started(model="gpt", reasoning_effort=None)
    process.feed = _turn_lines({"id": "turn-3", "result": {"turn": {"id": "turn-a"}}})
    turn = session.start_turn(TurnInput(prompt="read", model="gpt"))
    delta = {"method": "item/agentMessage/delta", "params": {"itemId": "m", "delta": "x" * 5000}}
    process.feed = _turn_lines(
        {
            "method": "item/started",
            "params": {"item": {"type": "agentMessage", "id": "m", "text": ""}},
        },
        delta,
        {
            "method": "item/completed",
            "params": {"item": {"type": "agentMessage", "id": "m", "text": "Done"}},
        },
        {
            "method": "item/started",
            "params": {"item": {"type": "commandExecution", "id": "c", "command": "ls"}},
        },
        {"method": "turn/completed", "params": {"turn": {"id": "turn-a", "status": "completed"}}},
    )
    events = list(turn.iter_events())
    messages = [event for event in events if event.event_type == "message"]
    assert len(messages) == 1
    assert messages[0].payload == {"text": "Done", "native_item_id": "m", "preview_id": "m"}
    fragments = [event for event in events if event.event_type == "raw"]
    assert len(fragments) == 2
    assert fragments[0].payload["protocol"] == "codex-app-server"
    # The answer's delta is a preview piece of the item the message names,
    # whole rather than a stored notification clipped at 4000 characters.
    assert fragments[1].payload == {
        "protocol": "reply-preview",
        "preview_id": "m",
        "delta": delta["params"]["delta"],
    }
    call = next(event for event in events if event.event_type == "tool_use")
    assert "usage_id" not in call.payload
    assert call.payload["id"] == "c"
    assert turn.wait().final_output == "Done"
