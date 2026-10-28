"""The gate: what a translated request may carry, and the Chat request it becomes."""

from __future__ import annotations

import json

import pytest

from cloud_agent_shared.protocol_compat.gate import CompatRejected, prepare


def _responses(**changes):
    return {
        "model": "m",
        "instructions": "Be brief.",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "dev"}],
            },
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
        "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"summary": "auto"},
        "prompt_cache_key": "client-cache",
        "client_metadata": {"device": "x"},
        "parallel_tool_calls": False,
        **changes,
    }


def _messages(**changes):
    return {
        "model": "m",
        "max_tokens": 1024,
        "stream": True,
        "system": [{"type": "text", "text": "You are Claude Code."}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "context_management": {"edits": []},
        "metadata": {"user_id": "device-identifier"},
        **changes,
    }


def test_a_responses_request_becomes_one_stateless_chat_request():
    chat = prepare("responses", _responses(), "m").chat
    assert chat["model"] == "m" and chat["stream"] is True
    assert chat["stream_options"] == {"include_usage": True}
    # Instructions and the developer message merge into one leading system message.
    assert [m["role"] for m in chat["messages"]] == ["system", "user"]
    assert chat["tools"] == [
        {"type": "function", "function": {"name": "exec_command", "parameters": {"type": "object"}}}
    ]
    assert chat["parallel_tool_calls"] is False
    for absent in ("store", "include", "reasoning", "prompt_cache_key", "client_metadata"):
        assert absent not in chat
    assert "reasoning_effort" not in chat


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"model": "other"}, "serves only the model"),
        ({"previous_response_id": "resp_1"}, "server-side state"),
        ({"conversation": "conv_1"}, "server-side state"),
        ({"background": True}, "Background"),
        ({"tools": [{"type": "web_search"}]}, "web_search"),
        ({"tools": [{"type": "custom", "name": "apply_patch"}]}, "custom"),
        ({"tools": [{"type": "local_shell"}]}, "local_shell"),
        (
            {
                "tools": [
                    {"type": "namespace", "name": "ns", "tools": [{"type": "custom", "name": "x"}]}
                ]
            },
            "custom",
        ),
        ({"tool_choice": {"type": "web_search"}}, "tool_choice"),
        (
            {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_file", "file_id": "f"}],
                    }
                ]
            },
            "input_file",
        ),
        ({"input": [{"type": "item_reference", "id": "x"}]}, "item_reference"),
        ({"input": [{"type": "custom_tool_call", "call_id": "c"}]}, "custom_tool_call"),
        ({"text": {"format": {"type": "grammar"}}}, "output formats"),
    ],
)
def test_a_responses_request_is_refused_rather_than_cut_down(changes, fragment):
    with pytest.raises(CompatRejected) as refused:
        prepare("responses", _responses(**changes), "m")
    assert refused.value.status == 400
    assert fragment in refused.value.message


def test_prior_reasoning_is_not_forwarded_but_calls_and_results_are():
    chat = prepare(
        "responses",
        _responses(
            input=[
                {"type": "message", "role": "user", "content": "go"},
                {"type": "reasoning", "encrypted_content": "opaque", "summary": []},
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "exec_command",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
            ]
        ),
        "m",
    ).chat
    assert "opaque" not in json.dumps(chat)
    roles = [m["role"] for m in chat["messages"]]
    assert roles[-2:] == ["assistant", "tool"]
    assert chat["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "done"}


def test_namespaced_tools_and_their_history_share_one_flat_name():
    namespace = {
        "type": "namespace",
        "name": "multi_agent_v1",
        "tools": [{"type": "function", "name": "spawn_agent", "parameters": {}}],
    }
    request = _responses(
        tools=[namespace],
        input=[
            {"type": "message", "role": "user", "content": "go"},
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "spawn_agent",
                "namespace": "multi_agent_v1",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ],
    )
    prepared = prepare("responses", request, "m")
    flat = prepared.chat["tools"][0]["function"]["name"]
    call = next(m for m in prepared.chat["messages"] if m.get("tool_calls"))["tool_calls"][0]
    assert flat == call["function"]["name"] == "multi_agent_v1__spawn_agent"
    assert prepared.names is not None
    assert prepared.names.original(flat) == ("multi_agent_v1", "spawn_agent")


def test_a_flat_name_never_collides_and_stays_within_chat_limits():
    long = "n" * 80
    request = _responses(
        tools=[
            {"type": "function", "name": "a__b", "parameters": {}},
            {"type": "namespace", "name": "a", "tools": [{"type": "function", "name": "b"}]},
            {"type": "namespace", "name": "x.y", "tools": [{"type": "function", "name": long}]},
        ]
    )
    prepared = prepare("responses", request, "m")
    names = [t["function"]["name"] for t in prepared.chat["tools"]]
    assert names[0] == "a__b"
    assert len(set(names)) == 3
    assert all(len(n) <= 64 and n.replace("_", "").replace("-", "").isalnum() for n in names)
    assert prepared.names is not None
    assert prepared.names.original(names[1]) == ("a", "b")
    assert prepared.names.original(names[2]) == ("x.y", long)


def test_a_messages_request_becomes_one_chat_request_without_client_metadata():
    chat = prepare("anthropic_messages", _messages(), "m").chat
    assert chat["model"] == "m" and chat["max_tokens"] == 1024
    assert chat["messages"][0] == {"role": "system", "content": "You are Claude Code."}
    assert chat["tools"][0]["function"]["name"] == "Bash"
    serialized = json.dumps(chat)
    for absent in ("device-identifier", "adaptive", "output_config", "context_management"):
        assert absent not in serialized
    assert "user" not in chat and "reasoning_effort" not in chat


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"model": "claude-opus"}, "serves only the model"),
        ({"tools": [{"type": "web_search_20250305", "name": "web_search"}]}, "web_search_20250305"),
        ({"tools": [{"type": "bash_20250124", "name": "bash"}]}, "bash_20250124"),
        ({"mcp_servers": [{"url": "https://x"}]}, "mcp_servers"),
        (
            {"messages": [{"role": "user", "content": [{"type": "document", "source": {}}]}]},
            "document",
        ),
        (
            {
                "messages": [
                    {"role": "assistant", "content": [{"type": "server_tool_use", "id": "s"}]}
                ]
            },
            "server_tool_use",
        ),
        (
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t",
                                "content": [{"type": "search_result"}],
                            }
                        ],
                    }
                ]
            },
            "search_result",
        ),
        (
            {
                "messages": [
                    {"role": "user", "content": [{"type": "image", "source": {"type": "file"}}]}
                ]
            },
            "images",
        ),
    ],
)
def test_a_messages_request_is_refused_rather_than_cut_down(changes, fragment):
    with pytest.raises(CompatRejected) as refused:
        prepare("anthropic_messages", _messages(**changes), "m")
    assert fragment in refused.value.message


def test_prior_thinking_is_not_forwarded_and_tool_choice_none_holds():
    chat = prepare(
        "anthropic_messages",
        _messages(
            tool_choice={"type": "none"},
            messages=[
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "secret plan", "signature": "sig"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a"}],
                },
            ],
        ),
        "m",
    ).chat
    assert "secret plan" not in json.dumps(chat)
    assert chat["tool_choice"] == "none"
    assert chat["messages"][-1] == {"role": "tool", "tool_call_id": "t1", "content": "a"}


def test_a_non_object_body_is_refused():
    with pytest.raises(CompatRejected):
        prepare("responses", ["not", "an", "object"], "m")


def test_a_digest_name_already_taken_is_refused_not_reused():
    from cloud_agent_shared.protocol_compat.namespaces import _digest

    long = "n" * 80
    taken = f"{('ns__' + long)[:52]}__{_digest('ns', long)}"
    request = _responses(
        tools=[
            {"type": "function", "name": taken, "parameters": {}},
            {"type": "namespace", "name": "ns", "tools": [{"type": "function", "name": long}]},
        ]
    )
    with pytest.raises(CompatRejected, match="share one name"):
        prepare("responses", request, "m")


def test_a_forced_namespaced_tool_choice_uses_the_flat_name_of_its_definition():
    request = _responses(
        tools=[
            {
                "type": "namespace",
                "name": "mcp__session",
                "tools": [{"type": "function", "name": "lookup", "parameters": {}}],
            }
        ],
        tool_choice={"type": "function", "name": "lookup", "namespace": "mcp__session"},
    )
    chat = prepare("responses", request, "m").chat
    flat = chat["tools"][0]["function"]["name"]
    assert chat["tool_choice"] == {"type": "function", "function": {"name": flat}}
