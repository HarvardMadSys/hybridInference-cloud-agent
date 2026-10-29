"""What the runtimes record for the Context events, from their pinned stream shapes.

Each record is something the CLI wrote on its stream anyway: no runtime is
asked anything, and nothing is inferred from a change in size.
"""

from __future__ import annotations

import json

import pytest
from tests.unit.test_live_sessions import _LiveProcess, _protocol_context, _turn_lines
from tests.unit.test_runner_host_settings import _PAYLOAD

from cloud_agent_host.drivers import TurnInput, driver_for
from cloud_agent_host.runner import ClaimedJob
from cloud_agent_host.runtimes import ClaudeCodeRuntime, KiloRuntime, OpencodeRuntime, PiRuntime


def _claude_user(content, **extra):
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}, **extra})


def test_claude_text_the_cli_added_is_an_injection_and_a_replay_is_not():
    runtime = ClaudeCodeRuntime()
    assert runtime.parse_event(_claude_user("my own words", isReplay=True)) is None
    event = runtime.parse_event(
        _claude_user([{"type": "text", "text": "Base directory for this skill: ..."}], isMeta=True)
    )
    assert event is not None and event.event_type == "lifecycle"
    assert event.payload == {
        "phase": "context_injection",
        "source": "claude_user_line",
        "label": "isMeta",
        "text": "Base directory for this skill: ...",
        "chars": 34,
    }
    long = runtime.parse_event(_claude_user("x" * 5000, parent_tool_use_id="toolu_task"))
    assert long is not None
    assert long.payload["chars"] == 5000 and len(long.payload["text"]) == 4000
    assert long.payload["truncated"] is True
    # A delegated agent's line keeps its owner, so it is not the conversation's.
    assert long.payload["parent_tool_use_id"] == "toolu_task"


def test_claude_tool_results_stay_tool_results():
    event = ClaudeCodeRuntime().parse_event(
        _claude_user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])
    )
    assert event is not None and event.event_type == "tool_result"


def test_pi_extension_message_is_an_injection():
    [event] = PiRuntime().parse_events(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "custom",
                    "customType": "rules",
                    "content": [{"type": "text", "text": "Follow the repo rules."}],
                    "display": False,
                },
            }
        )
    )
    assert event.event_type == "lifecycle"
    assert event.payload == {
        "phase": "context_injection",
        "source": "pi_custom_message",
        "label": "rules",
        "text": "Follow the repo rules.",
        "chars": 22,
    }


def _bridge_turn(tmp_path, runtime):
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver_for(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="go", model="deepseek-v4-flash"))
    return process, turn, json.loads(process.sent[-1])["id"]


def _event(turn_id, kind, properties):
    return {
        "bridge": "event",
        "turn_id": turn_id,
        "event": {"type": kind, "properties": properties},
    }


@pytest.mark.parametrize("runtime", [OpencodeRuntime, KiloRuntime])
def test_a_synthetic_user_part_is_one_injection_and_never_the_answer(tmp_path, runtime):
    process, turn, turn_id = _bridge_turn(tmp_path, runtime)

    def part(part_id, message_id, text, **extra):
        return _event(
            turn_id,
            "message.part.updated",
            {
                "part": {
                    "id": part_id,
                    "messageID": message_id,
                    "type": "text",
                    "text": text,
                    **extra,
                }
            },
        )

    process.feed = _turn_lines(
        # The part arrives before its message says whose it is.
        part("syn", "u1", "Summarize the task tool output above.", synthetic=True),
        _event(turn_id, "message.updated", {"info": {"id": "u1", "role": "user"}}),
        part("syn", "u1", "Summarize the task tool output above.", synthetic=True),
        _event(turn_id, "message.updated", {"info": {"id": "a1", "role": "assistant"}}),
        part("ans", "a1", "done"),
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )
    events = list(turn.iter_events())
    injections = [e for e in events if e.payload.get("phase") == "context_injection"]
    assert [e.payload["source"] for e in injections] == ["server_synthetic_part"]
    assert injections[0].payload["chars"] == len("Summarize the task tool output above.")
    assert turn.wait().final_output == "done"


@pytest.mark.parametrize("runtime", [OpencodeRuntime, KiloRuntime])
def test_a_pruned_tool_output_is_a_prune_once_and_not_a_new_result(tmp_path, runtime):
    process, turn, turn_id = _bridge_turn(tmp_path, runtime)

    def tool(state):
        return _event(
            turn_id,
            "message.part.updated",
            {"part": {"id": "p1", "callID": "c1", "type": "tool", "tool": "bash", "state": state}},
        )

    done = {"status": "completed", "input": {"cmd": "ls"}, "output": "x" * 300}
    pruned = {**done, "time": {"start": 1, "end": 2, "compacted": 3}}
    process.feed = _turn_lines(
        tool({"status": "running", "input": {"cmd": "ls"}}),
        tool(done),
        tool(pruned),
        tool(pruned),
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )
    events = list(turn.iter_events())
    assert sum(1 for e in events if e.event_type == "tool_result") == 1
    prunes = [e.payload for e in events if e.payload.get("phase") == "context_prune"]
    assert prunes == [
        {
            "phase": "context_prune",
            "source": f"{runtime.name}_prune",
            "tool": "bash",
            "call_id": "c1",
            "chars": 300,
        }
    ]


def test_a_claim_carries_bounded_injection_records_and_drops_the_rest():
    records = [
        {"producer": "memory", "items": 2, "chars": 50, "ascii": 50, "cjk": 0, "text": "secret"},
        {"producer": "skills", "names": [f"s{i}" * 100 for i in range(30)], "chars": 9},
        {"items": 1},
        "not a record",
        *({"producer": f"p{i}"} for i in range(20)),
    ]
    job = ClaimedJob.from_response({**_PAYLOAD, "context_injections": records})
    injections = job.context_injections
    assert len(injections) == 8  # ten looked at, two of them malformed
    assert injections[0] == {"producer": "memory", "items": 2, "chars": 50, "ascii": 50, "cjk": 0}
    assert len(injections[1]["names"]) == 20
    assert all(len(name) <= 120 for name in injections[1]["names"])
    assert ClaimedJob.from_response(_PAYLOAD).context_injections == ()
    assert (
        ClaimedJob.from_response({**_PAYLOAD, "context_injections": "x"}).context_injections == ()
    )
