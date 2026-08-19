"""The resident claude-code session (HOT, design §7): many turns, one process.

Driven with the real dialect adapter and authentic stream-json shapes, over a
fake live process — what is under test is the session machinery: lazy spawn
with the first turn's model, stdin-borne turns, result-bounded event slices,
settling without waiting for the process to die, and the restart refusal for
a mid-session model switch (D6).
"""

from __future__ import annotations

import json

import pytest

from cloud_agent_host.drivers import (
    TURN_COMPLETED,
    TURN_STARTED,
    ClaudeCodeDriver,
    DriverError,
    LaunchContext,
    TurnInput,
)
from cloud_agent_host.runtimes import EMPTY_RUNTIME_MCP_CONFIG, ClaudeCodeRuntime

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
        spawn=lambda argv, env: (_ for _ in ()).throw(AssertionError("one-shot spawn used")),
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


def test_close_ends_the_residency(tmp_path):
    process = _LiveProcess()
    session = ClaudeCodeDriver(ClaudeCodeRuntime()).launch_live(_live(process, tmp_path)[0])
    process.feed = _turn_lines(_RESULT)
    session.start_turn(TurnInput(prompt="p", model="m")).wait()

    session.close()

    assert process.stdin_closed and process.killed
    with pytest.raises(DriverError, match="closed"):
        session.start_turn(TurnInput(prompt="again", model="m"))


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
    with pytest.raises(DriverError, match="state mount"):
        ClaudeCodeDriver(ClaudeCodeRuntime()).launch_resident_live(context, session_id="s")


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
