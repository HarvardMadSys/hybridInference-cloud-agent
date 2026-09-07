"""Native OpenCode/Kilo events retain message ownership across the bridge."""

import json
import runpy
import threading
from pathlib import Path

import pytest

from cloud_agent_host.drivers import KiloDriver, OpenCodeDriver, TurnInput
from cloud_agent_host.runtimes import KiloRuntime, OpencodeRuntime
from tests.unit.test_live_sessions import _LiveProcess, _protocol_context, _turn_lines


def test_bridge_preserves_nested_message_metadata_only_for_its_session():
    script = Path(__file__).resolve().parents[2] / "deploy/sandbox/http-agent-live-bridge"
    bridge_type = runpy.run_path(str(script))["Bridge"]
    bridge = object.__new__(bridge_type)
    bridge.session_id = "session-a"
    bridge._turn_id = "turn-a"
    bridge._turn_lock = threading.Lock()
    emitted = []
    bridge.emit = emitted.append
    for session_id in ("session-other", "session-a"):
        bridge._event_payload(
            json.dumps(
                {
                    "type": "message.updated",
                    "properties": {
                        "info": {"id": "answer", "sessionID": session_id, "role": "assistant"}
                    },
                }
            )
        )
    assert len(emitted) == 1
    assert emitted[0]["turn_id"] == "turn-a"
    assert emitted[0]["event"]["properties"]["info"]["role"] == "assistant"


@pytest.mark.parametrize(
    "driver,runtime", [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
@pytest.mark.parametrize("metadata_first", [True, False])
def test_reply_excludes_user_unknown_and_stale_turn_parts(
    tmp_path, driver, runtime, metadata_first
):
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)

    for index in range(2):
        turn = session.start_turn(
            TurnInput(prompt=f"user question {index}", model="deepseek-v4-flash")
        )
        turn_id = json.loads(process.sent[-1])["id"]

        def event(kind, properties, owner=turn_id):
            return {
                "bridge": "event",
                "turn_id": owner,
                "event": {"type": kind, "properties": properties},
            }

        def message(message_id, role):
            return event("message.updated", {"info": {"id": message_id, "role": role}})

        def text(part_id, message_id, content, owner=turn_id):
            return event(
                "message.part.updated",
                {"part": {"id": part_id, "messageID": message_id, "type": "text", "text": content}},
                owner,
            )

        metadata = [message("user", "user"), message("answer", "assistant")]
        parts = [
            text("user-part", "user", f"user question {index}"),
            text("unknown-part", "unknown", "unattributed content"),
            text("answer-part", "answer", "partial"),
            text("answer-part", "answer", f"assistant answer {index}"),
            text("stale-part", "answer", "old turn content", "stale-turn"),
        ]
        process.feed = _turn_lines(
            *(metadata + parts if metadata_first else parts + metadata),
            {"bridge": "settled", "turn_id": "stale-turn", "status": "completed"},
            {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
        )
        assert turn.wait().final_output == f"assistant answer {index}"
