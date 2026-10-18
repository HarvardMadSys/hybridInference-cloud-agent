"""Regression coverage for bounded, visibly incomplete event payloads."""

import json

import pytest

from cloud_agent.schemas import WorkerEventRequest
from cloud_agent_host.runtimes import ClaudeCodeRuntime, NormalizedEvent

_LIMIT = 256 * 1024


def _size(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize("boundary", ["runtime", "api"])
@pytest.mark.parametrize(
    "content", ["a" * (2 * _LIMIT), '界😀\n"\\' * _LIMIT], ids=["ascii", "unicode-escapes"]
)
def test_large_payloads_are_bounded_in_json_bytes_and_keep_identity(boundary, content):
    original = {
        "tool_use_id": "tool-1",
        "content": content,
        "usage_id": "msg-1",
        "usage_scope": "attempt",
        "usage": {"input_tokens": 123, "output_tokens": 45},
        "is_error": False,
    }
    if boundary == "runtime":
        payload = NormalizedEvent("tool_result", original).payload
    else:
        payload = WorkerEventRequest(event_type="tool_result", payload=original).payload
    assert _size(payload) <= _LIMIT
    assert payload["truncated"] is True
    assert payload["original_payload_bytes"] == _size(original)
    assert "truncated" in payload["content"]
    assert payload["tool_use_id"] == original["tool_use_id"]
    assert payload["usage"] == original["usage"]
    assert payload["usage_id"] == original["usage_id"]
    assert payload["usage_scope"] == original["usage_scope"]
    assert payload["is_error"] is False
    assert original["content"] == content  # Do not alter the native record.


def test_payload_at_the_byte_limit_is_preserved_exactly():
    payload = {"text": ""}
    payload["text"] = "a" * (_LIMIT - _size(payload))
    assert _size(payload) == _LIMIT
    assert NormalizedEvent("message", payload).payload == payload


def test_large_nested_tool_input_and_combined_fields_are_bounded():
    payload = {
        "id": "tool-1",
        "name": "MCP",
        "input": {"images": [{"data": "x" * _LIMIT}] * 3},
        "content": "y" * _LIMIT,
    }
    bounded = NormalizedEvent("tool_use", payload).payload
    assert _size(bounded) <= _LIMIT
    assert bounded["truncated"] is True
    assert bounded["id"] == "tool-1"
    assert bounded["name"] == "MCP"


def test_oversized_keys_and_many_small_fields_cannot_bypass_the_limit():
    payload = {f"field-{index}": index for index in range(30_000)}
    payload["x" * _LIMIT] = "large key"
    bounded = WorkerEventRequest(event_type="raw", payload=payload).payload
    assert _size(bounded) <= _LIMIT
    assert bounded["truncated"] is True


def test_rechecking_a_bounded_payload_does_not_truncate_or_annotate_again():
    bounded = NormalizedEvent("message", {"text": "x" * (2 * _LIMIT)}).payload
    validated = WorkerEventRequest(event_type="message", payload=bounded).payload
    assert validated == bounded


def test_long_claude_answer_below_limit_remains_complete():
    text = "repository detail\n" * 500
    event = ClaudeCodeRuntime().parse_event(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    )
    assert event.payload["text"] == text
    assert "truncated" not in event.payload


#: ``cat /proc/self/cmdline`` and an emoji cut in half, as tool output carries
#: them: Postgres stores neither a NUL nor a surrogate, and UTF-8 has no surrogate.
_UNSTORABLE = "cat\x00/proc/self/cmdline\x00 \ud83d"
_STORED = "cat\ufffd/proc/self/cmdline\ufffd \ufffd"


@pytest.mark.parametrize("boundary", ["runtime", "api"])
def test_text_postgres_cannot_store_is_replaced_rather_than_refused(boundary):
    original = {
        "tool_use_id": "toolu_1",
        "content": _UNSTORABLE,
        "input": {"file_path\x00": ["/workspace/\udcff.txt"]},
    }
    if boundary == "runtime":
        payload = NormalizedEvent("tool_result", original).payload
    else:
        payload = WorkerEventRequest(event_type="tool_result", payload=original).payload
    assert payload == {
        "tool_use_id": "toolu_1",
        "content": _STORED,
        "input": {"file_path\ufffd": ["/workspace/\ufffd.txt"]},
    }
    assert _size(payload) > 0
    assert original["content"] == _UNSTORABLE  # Do not alter the native record.


def test_a_claude_tool_result_cut_inside_a_surrogate_pair_is_still_an_event():
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": _UNSTORABLE}
                ]
            },
        }
    )
    event = ClaudeCodeRuntime().parse_event(line)
    assert event is not None
    assert event.payload["content"] == _STORED
