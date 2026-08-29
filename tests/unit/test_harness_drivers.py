"""The Driver → Session → Turn contract (design §5, D9).

What is under test is the *platform-facing* layer: floor events around every
turn, the TurnResult guarantee that a completed turn always carries output,
one-turn-per-session in the replay world, and the v1 refusals (restore,
export, fork) that P2/P3 will replace with implementations.
"""

from __future__ import annotations

import pytest

from cloud_agent_host.drivers import (
    LINE,
    TURN_COMPLETED,
    TURN_FAILED,
    TURN_STARTED,
    DriverError,
    ForkUnsupported,
    HarnessCapabilities,
    HarnessDriver,
    LaunchContext,
    StateExportUnsupported,
    StateRestoreUnsupported,
    TurnInput,
    TurnResult,
    get_driver,
)
from cloud_agent_host.runtimes import EMPTY_RUNTIME_MCP_CONFIG, AgentRuntime, NormalizedEvent


class _Process:
    """A finished harness process with a scripted stdout."""

    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self._lines = lines
        self._exit_code = exit_code
        self.killed = False

    def lines(self):
        yield from self._lines

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return self._exit_code

    def stderr_text(self) -> str:
        return "stderr-tail"


class _Runtime(AgentRuntime):
    """Dialect stub: ``msg:``/``tool:`` lines parse, everything else does not."""

    name = "stub"

    def prepare(self, **kwargs):
        self.prepared = kwargs
        return ["stub-cli", kwargs["task_prompt"]], {"STUB": "1"}

    def parse_event(self, line: str):
        if line.startswith("msg:"):
            return NormalizedEvent("message", {"text": line[4:]})
        if line.startswith("tool:"):
            return NormalizedEvent("tool_use", {"name": line[5:]})
        return None


def _context(
    spawned: list[_Process] | None = None,
    *,
    state_dir: str | None = None,
    **process_kwargs,
) -> LaunchContext:
    def spawn(argv, env):
        process = _Process(**process_kwargs)
        process.argv, process.env = argv, env
        if spawned is not None:
            spawned.append(process)
        return process

    return LaunchContext(
        workdir="/w",
        gateway_base_url="http://gw",
        credential="agr-token",
        provides_isolation=True,
        spawn=spawn,
        base_env={"HOME": "/home/agent"},
        mcp_config=EMPTY_RUNTIME_MCP_CONFIG,
        state_dir=state_dir,
    )


def _session(**process_kwargs):
    return HarnessDriver(_Runtime()).launch(_context(**process_kwargs))


def test_a_turn_is_wrapped_in_floor_events_with_dialect_events_between():
    """turn_started first, turn_completed last, and nothing dropped between."""
    session = _session(lines=["msg:hello", "noise", "tool:ls"])
    turn = session.start_turn(TurnInput(prompt="do it", model="m"))

    events = list(turn.iter_events())

    assert [event.event_type for event in events] == [
        TURN_STARTED,
        "message",
        LINE,
        "tool_use",
        TURN_COMPLETED,
    ]
    assert events[2].raw_line == "noise"  # unparsed lines survive for the tail
    assert events[-1].payload["final_output"] == "hello"


def test_a_completed_tool_only_turn_still_carries_output():
    """D9: the driver synthesizes a displayable line, never an empty thread."""
    session = _session(lines=["tool:build"])
    turn = session.start_turn(TurnInput(prompt="p", model="m"))
    list(turn.iter_events())

    result = turn.wait()

    assert result.status == "completed"
    assert result.final_output.strip()


def test_the_result_contract_is_unrepresentable_to_break():
    with pytest.raises(ValueError, match="non-empty final_output"):
        TurnResult(status="completed", final_output="  ", exit_code=0)


def test_a_nonzero_exit_is_a_failed_floor_event():
    session = _session(lines=["msg:half done"], exit_code=3)
    turn = session.start_turn(TurnInput(prompt="p", model="m"))

    events = list(turn.iter_events())

    assert events[-1].event_type == TURN_FAILED
    assert events[-1].payload == {"status": "failed", "exit_code": 3}


def test_cancel_kills_the_process_and_settles_as_cancelled():
    session = _session(lines=[])
    turn = session.start_turn(TurnInput(prompt="p", model="m"))

    turn.cancel()

    assert turn.wait().status == "cancelled"


def test_prepare_receives_the_launch_context_and_the_turn_input():
    """The dialect layer keeps its three questions; the session asks them."""
    spawned: list[_Process] = []
    driver = HarnessDriver(_Runtime())
    session = driver.launch(_context(spawned=spawned, lines=[]))

    session.start_turn(TurnInput(prompt="fix the bug", model="glm-5.1"))

    prepared = driver.runtime.prepared
    assert prepared["task_prompt"] == "fix the bug"
    assert prepared["model"] == "glm-5.1"
    assert prepared["credential"] == "agr-token"
    (process,) = spawned
    assert process.argv == ["stub-cli", "fix the bug"]
    assert process.env == {"HOME": "/home/agent", "STUB": "1"}  # base then dialect


def test_a_replay_session_runs_exactly_one_turn():
    session = _session(lines=[])
    first = session.start_turn(TurnInput(prompt="p", model="m"))
    with pytest.raises(DriverError, match="already running"):
        session.start_turn(TurnInput(prompt="again", model="m"))
    first.wait()

    with pytest.raises(DriverError, match="one turn per session"):
        session.start_turn(TurnInput(prompt="again", model="m"))


def test_close_kills_a_running_turn_and_refuses_new_ones():
    spawned: list[_Process] = []
    session = HarnessDriver(_Runtime()).launch(_context(spawned=spawned, lines=[]))
    session.start_turn(TurnInput(prompt="p", model="m"))

    session.close()

    assert spawned[0].killed is True
    with pytest.raises(DriverError, match="closed"):
        session.start_turn(TurnInput(prompt="p", model="m"))


def test_the_v1_refusals_are_loud_and_typed():
    """Restore, export and fork refuse now; P2/P3 replace these, not callers."""
    driver = HarnessDriver(_Runtime())
    with pytest.raises(StateRestoreUnsupported):
        driver.launch(_context(lines=[]), state=b"blob")
    session = driver.launch(_context(lines=[]))
    with pytest.raises(StateExportUnsupported):
        session.export_state()
    with pytest.raises(ForkUnsupported):
        session.fork()


def test_capabilities_report_the_replay_world_we_actually_run():
    """Honest values: continuity flips to resume/hot only when P2/P3 wire it."""
    capabilities = HarnessDriver(_Runtime()).capabilities()

    assert capabilities == HarnessCapabilities(
        continuity="replay", steering="none", fork="none", model_switch="per_turn"
    )


def test_get_driver_resolves_the_registry_and_keeps_its_refusal():
    assert get_driver("claude-code").id == "claude-code"
    with pytest.raises(KeyError):
        get_driver("no-such-harness")


@pytest.mark.parametrize(
    ("runtime_id", "resume_flag"),
    [
        ("codex", "resume"),
        ("pi", "--session"),
        ("opencode", "--session"),
        ("kilo", "--session"),
    ],
)
def test_native_resume_drivers_pass_only_the_new_prompt_and_exact_session_id(
    runtime_id: str,
    resume_flag: str,
):
    """A follow-up uses the CLI's native locator, never transcript replay."""
    spawned: list[_Process] = []
    driver = get_driver(runtime_id)
    session = driver.launch_resident(
        _context(spawned=spawned, state_dir="/host-only-state", lines=[]),
        session_id="native-session-123",
    )

    session.start_turn(TurnInput(prompt="only the new message", model="glm-5.1"))

    (process,) = spawned
    assert resume_flag in process.argv
    assert process.argv[process.argv.index(resume_flag) + 1] == "native-session-123"
    assert "only the new message" in process.argv
    # The host path is never exposed inside the sandbox; all four CLIs use
    # the canonical state mount / Docker-layer path.
    assert "/host-only-state" not in process.argv
    assert "/host-only-state" not in process.env.values()
    assert driver.capabilities().continuity == "resume"
    assert driver.capabilities().checkpoint_export is False
