"""What a model request sends ahead of the conversation, as the member's meter measures it."""

from __future__ import annotations

import json
import os

import pytest

from cloud_agent_host.protocol_translator import MAX_PENDING_RECORDS, PrefixMeter, RecordWriter
from cloud_agent_shared.request_prefix import (
    MAX_RECORD_BYTES,
    MAX_TOOL_ENTRIES,
    RECORD_MARKER,
    measure,
    protocol_for_path,
    read_record,
    record_line,
)

_TOOL = {
    "name": "Read",
    "description": "Read a file",
    "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
}


def _line(record: bytes) -> str:
    return record.decode().rstrip("\n")


@pytest.mark.parametrize(
    ("path", "protocol"),
    [
        ("/v1/messages", "anthropic_messages"),
        ("/anthropic/v1/messages?beta=true", "anthropic_messages"),
        ("/v1/responses", "responses"),
        ("/api/v1/chat/completions", "chat_completions"),
        ("/v1/messages/count_tokens", None),
        ("/v1/responses/compact", None),
        ("/v1/models", None),
    ],
)
def test_only_a_model_endpoint_is_measured(path, protocol):
    assert protocol_for_path(path) == protocol


def test_anthropic_system_blocks_and_tools_are_counted_apart_from_the_conversation():
    body = {
        "model": "claude-x",
        "system": [{"type": "text", "text": "You are 助手."}, {"type": "text", "text": "Be brief"}],
        "tools": [_TOOL],
        "messages": [{"role": "user", "content": "a long conversation " * 100}],
    }
    record = measure("anthropic_messages", body, mode="passthrough")
    assert record is not None
    assert record["model"] == "claude-x"
    assert record["system"] == {"chars": 19, "ascii": 17, "cjk": 2, "parts": 2}
    compact = json.dumps(_TOOL, ensure_ascii=False, separators=(",", ":"))
    assert record["tools"] == {"count": 1, "chars": len(compact), "ascii": len(compact), "cjk": 0}
    assert record["entries"] == [["Read", len(compact), len(compact), 0]]


def test_responses_instructions_and_opening_developer_messages_are_the_system_prompt():
    body = {
        "model": "gpt-x",
        "instructions": "base",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "dev"}],
            },
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "message", "role": "developer", "content": "later, in the conversation"},
        ],
        "tools": [{"type": "function", "name": "shell", "parameters": {}}, {"type": "web_search"}],
    }
    record = measure("responses", body, mode="translated")
    assert record["system"]["chars"] == len("base") + len("dev")
    assert record["system"]["parts"] == 2
    assert sorted(name for name, *_ in record["entries"]) == ["shell", "web_search"]
    assert record["mode"] == "translated"


def test_chat_completions_system_is_the_opening_system_messages():
    body = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "one"},
            {"role": "system", "content": [{"type": "text", "text": "two"}]},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "a reminder mid-conversation"},
        ],
        "tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}],
    }
    record = measure("chat_completions", body, mode="passthrough")
    assert record["system"]["chars"] == 6
    assert record["entries"][0][0] == "bash"


def test_a_body_that_is_no_request_is_not_measured():
    assert measure("anthropic_messages", ["not", "an", "object"], mode="passthrough") is None
    assert measure("other", {}, mode="passthrough") is None
    record = measure("chat_completions", {"messages": "x", "tools": "y"}, mode="passthrough")
    assert record["system"]["chars"] == 0 and record["tools"]["count"] == 0


def test_the_largest_tools_are_named_and_the_rest_counted():
    tools = [{"name": f"t{index}", "description": "x" * index} for index in range(250)]
    record = measure("anthropic_messages", {"tools": tools}, mode="passthrough")
    assert record["tools"]["count"] == 250
    assert len(record["entries"]) == MAX_TOOL_ENTRIES
    assert record["entries_omitted"] == 50
    assert record["entries"][0][0] == "t249"
    assert len(record_line(record)) < MAX_RECORD_BYTES


def test_a_prefix_is_recorded_whole_once_then_repeated():
    meter = PrefixMeter(mode="passthrough")
    body = {"system": "s", "tools": [_TOOL], "messages": []}
    first = read_record(_line(meter.line("anthropic_messages", body)))
    again = read_record(_line(meter.line("anthropic_messages", {**body, "messages": [1]})))
    other = read_record(_line(meter.line("anthropic_messages", {**body, "system": "t"})))
    assert first["system"]["chars"] == 1 and "repeat" not in first
    # The conversation is not the prefix: a later request repeats it.
    assert again == {"fp": first["fp"], "repeat": True}
    assert other["fp"] != first["fp"] and "repeat" not in other


def test_a_record_that_was_dropped_is_written_whole_next_time():
    meter = PrefixMeter(mode="passthrough")
    body = {"system": "s", "tools": [_TOOL], "messages": []}
    written: list[bytes] = []
    meter.report("anthropic_messages", body, lambda line: False)
    for _ in range(2):
        meter.report("anthropic_messages", body, lambda line: written.append(line) or True)
    first, again = (read_record(_line(line)) for line in written)
    # A repeat of a record the host never got would resolve to nothing.
    assert "repeat" not in first and again == {"fp": first["fp"], "repeat": True}


def test_a_record_never_waits_on_a_stderr_nobody_reads():
    read_end, write_end = os.pipe()
    writer = RecordWriter(write_end)
    line = b"x" * 70_000 + b"\n"  # more than a pipe holds: the writer blocks on it
    queued = [writer.put(line) for _ in range(MAX_PENDING_RECORDS + 3)]
    # Every put returned at once; past the queue's bound, records were dropped.
    assert queued[0] and not all(queued)
    received = b""
    while len(received) < len(line):
        received += os.read(read_end, len(line) - len(received))
    assert received == line
    # The write end stays open: the writer thread still holds it, and a closed
    # number could be reused by another test's file.
    os.close(read_end)


def test_a_record_line_round_trips_and_anything_else_is_refused():
    record = measure("anthropic_messages", {"system": "s", "tools": [_TOOL]}, mode="passthrough")
    line = _line(record_line(record))
    read = read_record(line)
    assert read == {key: value for key, value in record.items() if key != "v"}
    assert read_record("plain stderr text") is None
    assert read_record(RECORD_MARKER + "{not json") is None
    assert read_record(RECORD_MARKER + json.dumps({**record, "v": 2})) is None
    assert read_record(RECORD_MARKER + json.dumps({**record, "fp": "../x"})) is None
    bad = {**record, "system": {**record["system"], "chars": -1}}
    assert read_record(RECORD_MARKER + json.dumps(bad)) is None
    assert read_record(RECORD_MARKER + json.dumps({**record, "entries": [["x", 1, 1]]})) is None
    assert read_record(RECORD_MARKER + json.dumps({**record, "protocol": "smtp"})) is None
    huge = {**record, "model": "m" * MAX_RECORD_BYTES}
    assert read_record(RECORD_MARKER + json.dumps(huge)) is None
