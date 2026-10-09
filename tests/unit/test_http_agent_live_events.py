"""Native OpenCode/Kilo events retain message ownership across the bridge."""

import json
import runpy
import threading
from pathlib import Path

import pytest
from tests.unit.test_live_sessions import _LiveProcess, _protocol_context, _turn_lines

from cloud_agent_host.drivers import KiloDriver, OpenCodeDriver, TurnInput
from cloud_agent_host.runtimes import KiloRuntime, OpencodeRuntime


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


@pytest.mark.parametrize(
    "driver,runtime", [(OpenCodeDriver, OpencodeRuntime), (KiloDriver, KiloRuntime)]
)
def test_the_answer_is_previewed_and_each_part_completes_in_its_place(tmp_path, driver, runtime):
    """The pinned 1.18.9 shape: an empty part, a delta per token, the whole part."""
    process = _LiveProcess()
    context, _ = _protocol_context(process, tmp_path)
    process.feed = _turn_lines({"bridge": "ready", "session_id": "session-a"})
    session = driver(runtime()).launch_live(context)
    session.ensure_started(model="deepseek-v4-flash", reasoning_effort=None)
    turn = session.start_turn(TurnInput(prompt="look", model="deepseek-v4-flash"))
    turn_id = json.loads(process.sent[-1])["id"]

    def event(kind, properties):
        return {
            "bridge": "event",
            "turn_id": turn_id,
            "event": {"type": kind, "properties": properties},
        }

    def message(message_id, role):
        return event("message.updated", {"info": {"id": message_id, "role": role}})

    def part(part_id, message_id, kind, text, **extra):
        return event(
            "message.part.updated",
            {"part": {"id": part_id, "messageID": message_id, "type": kind, "text": text, **extra}},
        )

    def delta(part_id, message_id, text):
        return event(
            "message.part.delta",
            {"messageID": message_id, "partID": part_id, "field": "text", "delta": text},
        )

    def tool(status):
        return event(
            "message.part.updated",
            {
                "part": {
                    "id": "tool-part",
                    "callID": "call-1",
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": status, "input": {"command": "pwd"}, "output": "/w"},
                }
            },
        )

    process.feed = _turn_lines(
        message("user", "user"),
        part("user-part", "user", "text", "look"),
        message("answer", "assistant"),
        part("thought", "answer", "reasoning", "", time={"start": 1}),
        delta("thought", "answer", "hmm"),
        part("thought", "answer", "reasoning", "hmm", time={"start": 1, "end": 2}),
        part("first", "answer", "text", "", time={"start": 2}),
        delta("first", "answer", "Let me"),
        delta("first", "answer", " check."),
        part("first", "answer", "text", "Let me check.", time={"start": 2, "end": 3}),
        tool("running"),
        tool("completed"),
        # Metadata after its part: previewed once the owner is known.
        part("second", "late", "text", ""),
        delta("second", "late", "All"),
        message("late", "assistant"),
        delta("second", "late", " done."),
        part("second", "late", "text", "All done.", time={"start": 4, "end": 5}),
        {"bridge": "settled", "turn_id": turn_id, "status": "completed"},
    )
    events = list(turn.iter_events())

    previews = [
        (event.payload["preview_id"], event.payload["delta"])
        for event in events
        if event.payload.get("protocol") == "reply-preview"
    ]
    # Neither the user's text nor reasoning is previewed; a finished part is
    # shown whole; a late owner's part is previewed with everything it had.
    assert [preview_id for preview_id, _ in previews] == ["first", "first", "second"]
    assert "".join(text for preview_id, text in previews if preview_id == "first") == (
        "Let me check."
    )
    assert previews[-1] == ("second", "All done.")
    shown = [
        "preview" if event.payload.get("protocol") == "reply-preview" else event.event_type
        for event in events
        if event.event_type in {"thinking", "tool_use", "tool_result", "message"}
        or event.payload.get("protocol") == "reply-preview"
    ]
    assert shown == [
        "thinking",
        "preview",
        "preview",
        "tool_use",
        "tool_result",
        "preview",
        "message",
        "message",
    ]
    answer = [event.payload for event in events if event.event_type == "message"]
    assert answer == [
        {"text": "Let me check.", "preview_id": "first"},
        {"text": "All done.", "preview_id": "second"},
    ]
    assert turn.wait().final_output == "Let me check.\n\nAll done."
