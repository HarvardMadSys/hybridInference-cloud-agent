"""Only native query completion proves that failed-turn history is reusable."""

import json

import pytest

from cloud_agent_host.drivers import DriverError, get_driver
from tests.unit.test_live_sessions import _LiveProcess, _protocol_context


def _turn(tmp_path, runtime, frames):
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    session = get_driver(runtime).launch_live(context)
    # Isolate stream settlement from the independently tested RPC handshake.
    session._process = process
    session._lines = iter(json.dumps(frame) for frame in frames)
    turn = session._new_turn()
    turn.protocol_turn_id = "turn-a"
    return turn


@pytest.mark.parametrize("runtime", ["codex", "pi", "opencode", "kilo"])
def test_native_api_failure_is_failed_but_retains_completed_boundary(tmp_path, runtime):
    frame = {
        "codex": {
            "method": "turn/completed",
            "params": {
                "turn": {"id": "turn-a", "status": "failed", "error": {"message": "API rejected"}}
            },
        },
        "pi": {
            "type": "agent_end",
            "messages": [
                {"role": "assistant", "stopReason": "toolUse"},
                {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "API rejected",
                    "content": [],
                },
            ],
        },
    }.get(runtime, {"bridge": "settled", "turn_id": "turn-a", "status": "failed"})
    turn = _turn(tmp_path, runtime, [frame])
    result = turn.wait()
    assert result.status == "failed"
    assert result.exit_code != 0
    assert result.final_output == ""
    assert result.native_turn_settled is True


@pytest.mark.parametrize("runtime", ["pi", "opencode", "kilo"])
def test_command_rejection_does_not_prove_a_native_turn_completed(tmp_path, runtime):
    frame = (
        {"type": "response", "id": "turn-a", "success": False, "error": "not admitted"}
        if runtime == "pi"
        else {"bridge": "command_error", "id": "turn-a", "detail": "not admitted"}
    )
    result = _turn(tmp_path, runtime, [frame]).wait()
    assert result.status == "failed"
    assert result.native_turn_settled is False


@pytest.mark.parametrize("runtime", ["codex", "pi", "opencode", "kilo"])
def test_transport_eof_does_not_forge_a_native_result(tmp_path, runtime):
    turn = _turn(tmp_path, runtime, [])
    with pytest.raises(DriverError, match="exited unexpectedly"):
        turn.wait()
    assert turn._result is None


def test_pi_reports_terminal_error_and_allows_a_recovered_final_assistant(tmp_path):
    from cloud_agent_host.runtimes import PiRuntime

    error = {"role": "assistant", "stopReason": "error", "errorMessage": "API rejected"}
    failed = {"type": "agent_end", "messages": [error]}
    assert PiRuntime().parse_event(json.dumps(failed)).event_type == "error"
    recovered = {
        "type": "agent_end",
        "messages": [error, {"role": "assistant", "stopReason": "stop"}],
    }
    result = _turn(tmp_path, "pi", [recovered]).wait()
    assert result.status == "completed"
    assert result.native_turn_settled is True
