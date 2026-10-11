"""Unit tests for agent runtime adapters.

The Claude Code cases replay a sanitized, synthetic contract fixture derived
from observed ``claude -p --output-format stream-json`` output. This pins the
parser to the CLI's emitted shape without checking in a real machine's runtime
metadata.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cloud_agent_host.runtimes import (
    MAX_INIT_TOOLS,
    MERGED_MCP_SERVER_NAME,
    REPLY_PREVIEW,
    ClaudeCodeRuntime,
    CodexRuntime,
    GenericRuntime,
    KiloRuntime,
    OpencodeRuntime,
    PiRuntime,
    ReplyPreview,
    RuntimeEffortUnsupportedError,
    RuntimeMCPConfig,
    RuntimeMCPUnavailableError,
    get_runtime,
    runtime_reasoning_efforts,
    runtimes_supporting_mcp,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agent_runtime_streams"


def _contract_lines() -> list[str]:
    """Return the synthetic Claude Code contract stream, line by line."""
    return (_FIXTURES / "claude_code_stream_contract.jsonl").read_text().splitlines()


def test_synthetic_contract_stream_maps_to_the_normalized_kinds():
    """The sanitized contract stream preserves the six observed event kinds."""
    runtime = ClaudeCodeRuntime()
    kinds = [
        event.event_type
        for line in _contract_lines()
        if (event := runtime.parse_event(line)) is not None
    ]
    # init -> tool_use -> rate-limit notice -> tool_result -> answer -> result
    assert kinds == [
        "lifecycle",
        "tool_use",
        "lifecycle",
        "tool_result",
        "message",
        "lifecycle",
    ]
    assert "raw" not in kinds, "synthetic contract stream should be fully recognized"


def test_tool_use_carries_name_and_input():
    """The contract's synthetic tool call is reported with its arguments."""
    runtime = ClaudeCodeRuntime()
    events = [runtime.parse_event(line) for line in _contract_lines()]
    tool_use = next(e for e in events if e and e.event_type == "tool_use")
    assert tool_use.payload["name"] == "bash"
    assert tool_use.payload["input"] == {"cmd": "pwd"}


def test_final_result_reports_cost_as_untrusted():
    """The runtime's self-reported cost is exposed but labelled as such."""
    runtime = ClaudeCodeRuntime()
    events = [runtime.parse_event(line) for line in _contract_lines()]
    result = [e for e in events if e and e.event_type == "lifecycle"][-1]
    assert result.payload["phase"] == "result"
    assert "PWD_OK" in result.payload["text"]
    # Present for observability; api_logs remains the billing authority.
    assert result.payload["reported_cost_usd"] is not None


def test_unrecognized_lines_become_raw_not_dropped():
    """A format change degrades to a log tail instead of silence."""
    runtime = ClaudeCodeRuntime()
    for line in ('{"type":"brand_new_thing","x":1}', "not json at all", "[1,2,3]"):
        event = runtime.parse_event(line)
        assert event is not None
        assert event.event_type == "raw"
        assert event.payload["text"]


def test_blank_lines_are_skipped():
    """Empty output produces no event at all."""
    assert ClaudeCodeRuntime().parse_event("   ") is None


def test_error_result_maps_to_error():
    """A failed run surfaces as an error event, not a successful lifecycle."""
    line = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})
    event = ClaudeCodeRuntime().parse_event(line)
    assert event.event_type == "error"
    assert event.payload["subtype"] == "error_max_turns"


@pytest.mark.parametrize("is_error", [None, False])
def test_terminal_api_failure_overrides_the_success_subtype(is_error):
    failure = {
        "type": "result",
        "subtype": "success",
        "terminal_reason": "api_error",
        "api_error_status": 503,
        "result": "API Error: 503 No provider is currently available for model 'kimi-k3'.",
        "usage": {"input_tokens": 5},
    }
    if is_error is not None:
        failure["is_error"] = is_error
    event = ClaudeCodeRuntime().parse_event(json.dumps(failure))
    assert event.event_type == "error"
    assert event.payload["terminal_reason"] == "api_error"
    assert event.payload["api_error_status"] == 503
    assert event.payload["text"] == failure["result"]
    assert event.payload["usage"] == {"input_tokens": 5}
    assert event.payload["usage_scope"] == "attempt"


def test_successful_terminal_reason_remains_a_result():
    event = ClaudeCodeRuntime().parse_event(
        json.dumps({"type": "result", "subtype": "success", "terminal_reason": "end_turn"})
    )
    assert event.event_type == "lifecycle"
    assert event.payload["phase"] == "result"


def test_api_retry_notices_report_each_attempt_and_delay():
    runtime = ClaudeCodeRuntime()
    for attempt, delay_ms in [(1, 5000), (2, 10000)]:
        event = runtime.parse_event(
            json.dumps(
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "attempt": attempt,
                    "max_retries": 10,
                    "retry_delay_ms": delay_ms,
                    "error_status": 503,
                }
            )
        )
        assert event.event_type == "lifecycle"
        assert event.payload == {
            "phase": "api_retry",
            "attempt": attempt,
            "max_retries": 10,
            "retry_delay_ms": delay_ms,
            "error_status": 503,
        }


@pytest.mark.parametrize("kind", ["text", "thinking", "tool_result", "result"])
def test_claude_transcript_preserves_content_past_4000_characters(kind):
    """A long answer or tool result must survive normalization in full."""
    content = "repository detail\n" * 500 + "END OF CONTENT"
    if kind == "result":
        raw = {"type": "result", "result": content}
        key = "text"
    elif kind == "tool_result":
        raw = {
            "type": "user",
            "message": {"content": [{"type": kind, "content": content}]},
        }
        key = "content"
    else:
        raw = {
            "type": "assistant",
            "message": {"content": [{"type": kind, kind: content}]},
        }
        key = "text"
    event = ClaudeCodeRuntime().parse_event(json.dumps(raw))
    assert event.payload[key] == content


@pytest.mark.parametrize("kind", ["text", "thinking", "tool_use"])
def test_claude_assistant_blocks_keep_the_response_identity(kind):
    """Parallel blocks repeat one model response's usage, not separate calls."""
    usage = {"input_tokens": 100, "output_tokens": 0}
    event = ClaudeCodeRuntime().parse_event(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "msg_shared",
                    "content": [{"type": kind, kind: "content"}],
                    "usage": usage,
                },
            }
        )
    )
    assert event.payload["usage_id"] == "msg_shared"
    assert event.payload["usage"] == usage


@pytest.mark.parametrize("kind", ["text", "thinking", "tool_use"])
def test_claude_subagent_blocks_keep_the_parent_call_identity(kind):
    """Child activity retains its recorded owner without changing native ids."""
    block = {"type": kind, kind: "Child reply"}
    if kind == "tool_use":
        block = {"type": kind, "id": "child-tool", "name": "Read", "input": {"path": "a.py"}}
    event = ClaudeCodeRuntime().parse_event(
        json.dumps(
            {
                "type": "assistant",
                "parent_tool_use_id": "parent-agent-call",
                "message": {
                    "id": "child-message",
                    "content": [block],
                    "usage": {"input_tokens": 3},
                },
            }
        )
    )
    assert event.payload["parent_tool_use_id"] == "parent-agent-call"
    assert event.payload["usage_id"] == "child-message"
    if kind == "tool_use":
        assert event.payload["id"] == "child-tool"
        assert event.payload["input"] == {"path": "a.py"}
    else:
        assert event.payload["text"] == "Child reply"
        assert "preview_id" not in event.payload


def test_claude_subagent_tool_result_keeps_the_parent_call_identity():
    """Results remain paired to the child tool and attributable to its agent."""
    event = ClaudeCodeRuntime().parse_event(
        json.dumps(
            {
                "type": "user",
                "parent_tool_use_id": "parent-agent-call",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "child-tool",
                            "content": "Child tool output",
                            "is_error": True,
                        }
                    ]
                },
            }
        )
    )
    assert event.payload == {
        "tool_use_id": "child-tool",
        "is_error": True,
        "content": "Child tool output",
        "parent_tool_use_id": "parent-agent-call",
    }


@pytest.mark.parametrize("parent", [None, "", 1, {"id": "untrusted"}])
@pytest.mark.parametrize("kind", ["assistant", "user"])
def test_claude_does_not_invent_a_parent_call_identity(parent, kind):
    """Only a nonempty recorded string identifies a parent agent call."""
    block = (
        {"type": "tool_use", "id": "t", "name": "Read", "input": {}}
        if kind == "assistant"
        else {"type": "tool_result", "tool_use_id": "t", "content": "ok"}
    )
    event = ClaudeCodeRuntime().parse_event(
        json.dumps({"type": kind, "parent_tool_use_id": parent, "message": {"content": [block]}})
    )
    assert "parent_tool_use_id" not in event.payload


@pytest.mark.parametrize("is_error", [False, True])
def test_claude_result_keeps_the_completed_turn_usage(is_error):
    """Success and failure both carry usage that supersedes streamed estimates."""
    usage = {"input_tokens": 42, "output_tokens": 7, "cache_read_input_tokens": 100}
    event = ClaudeCodeRuntime().parse_event(
        json.dumps({"type": "result", "is_error": is_error, "usage": usage})
    )
    assert event.payload["usage"] == usage
    assert event.payload["usage_scope"] == "attempt"


def _partial_stream_lines() -> list[str]:
    """The pinned 2.1.220 with ``--include-partial-messages``, sanitized.

    Captured against a fake gateway: one response streams a thinking block, a
    text block and a Bash call; the next streams a five-piece answer.
    """
    return (_FIXTURES / "claude_code_partial_stream_contract.jsonl").read_text().splitlines()


class _Clock:
    """A monotonic clock that moves only when read, by ``step``."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _pieces(events) -> list[tuple[str, str]]:
    return [
        (event.payload["preview_id"], event.payload["delta"])
        for event in events
        if event.event_type == "raw" and event.payload.get("protocol") == REPLY_PREVIEW
    ]


def test_a_preview_reports_the_first_piece_at_once_and_then_batches():
    clock = _Clock()
    preview = ReplyPreview(clock)
    assert preview.add("a", "Hel") == {"protocol": REPLY_PREVIEW, "preview_id": "a", "delta": "Hel"}
    clock.now = 0.1
    assert preview.add("a", "lo ") is None
    clock.now = 0.2
    assert preview.add("a", "there")["delta"] == "lo there"
    # Each answer has its own interval; another one starts at once.
    assert preview.add("b", "x")["delta"] == "x"
    assert preview.add("a", "!") is None
    # Text that is complete goes out without waiting.
    assert preview.flush("a")["delta"] == "!"
    assert preview.flush("a") is None


def test_a_preview_follows_a_harness_that_resends_the_whole_text():
    preview = ReplyPreview(_Clock(step=1.0))
    assert preview.grow_to("p", "Hello")["delta"] == "Hello"
    assert preview.grow_to("p", "Hello world")["delta"] == " world"
    assert preview.grow_to("p", "Hello world") is None
    # Rewritten instead of extended: the preview starts again from the new text.
    reset = preview.grow_to("p", "Goodbye")
    assert reset == {
        "protocol": REPLY_PREVIEW,
        "preview_id": "p",
        "delta": "Goodbye",
        "reset": True,
    }
    preview.finish("p")
    assert preview.grow_to("p", "Goodbye now")["delta"] == "Goodbye now"


def test_claude_asks_for_the_answer_while_it_is_written():
    """Both transports carry the flag that makes the CLI stream its text."""
    runtime = ClaudeCodeRuntime()
    common = {
        "workdir": "/tmp/x",
        "model": "glm-5.1",
        "gateway_base_url": "https://gateway.example.com/v1",
        "credential": "ajt.a.b",
    }
    argv, _ = runtime.prepare(task_prompt="do the thing", **common)
    live, _ = runtime.prepare_live(**common)
    assert "--include-partial-messages" in argv
    assert "--include-partial-messages" in live


def test_claude_previews_the_answer_text_and_nothing_else():
    """Only the answer streams; the complete blocks arrive as before and name it."""
    runtime = ClaudeCodeRuntime(clock=_Clock(step=1.0))
    events = [
        event
        for line in _partial_stream_lines()
        if (event := runtime.parse_event(line)) is not None
    ]
    assert _pieces(events) == [
        ("msg_1:1", "Let me "),
        ("msg_1:1", "look at "),
        ("msg_1:1", "the workspace."),
        ("msg_2:0", "The "),
        ("msg_2:0", "workspace "),
        ("msg_2:0", "is "),
        ("msg_2:0", "here. "),
        ("msg_2:0", "Done."),
    ]
    # Thinking, a tool call's input and the message frame are consumed, not
    # stored: the normalized transcript is the one the CLI gave without the flag.
    assert [event.event_type for event in events if event.event_type != "raw"] == [
        "thinking",
        "message",
        "tool_use",
        "tool_result",
        "message",
        "lifecycle",
    ]
    messages = [event.payload for event in events if event.event_type == "message"]
    assert [(m["text"], m["preview_id"], m["usage_id"]) for m in messages] == [
        ("Let me look at the workspace.", "msg_1:1", "msg_1"),
        ("The workspace is here. Done.", "msg_2:0", "msg_2"),
    ]


def test_claude_batches_pieces_and_drops_the_rest_at_the_complete_block():
    runtime = ClaudeCodeRuntime(clock=_Clock(step=0.0))
    events = [
        event
        for line in _partial_stream_lines()
        if (event := runtime.parse_event(line)) is not None
    ]
    # With the clock standing still only the first piece of each block is due;
    # the complete message carries the rest.
    assert _pieces(events) == [("msg_1:1", "Let me "), ("msg_2:0", "The ")]


def test_a_preview_piece_is_not_a_model_response():
    """A piece carries no usage or ``usage_id`` for accounting to count."""
    runtime = ClaudeCodeRuntime(clock=_Clock(step=1.0))
    pieces = [
        event
        for line in _partial_stream_lines()
        if (event := runtime.parse_event(line)) is not None and event.event_type == "raw"
    ]
    assert pieces
    for event in pieces:
        assert set(event.payload) == {"protocol", "preview_id", "delta"}


def test_a_claude_subagents_text_is_not_previewed():
    """A subagent's answer is reported by its complete lines only."""
    runtime = ClaudeCodeRuntime(clock=_Clock(step=1.0))
    for line in _partial_stream_lines():
        record = json.loads(line)
        if record["type"] == "stream_event":
            record["parent_tool_use_id"] = "toolu_task"
            assert runtime.parse_event(json.dumps(record)) is None


def _pi_contract_lines() -> list[str]:
    """Return the sanitized pi ``--mode json`` stream, line by line."""
    return (_FIXTURES / "pi_stream_contract.jsonl").read_text().splitlines()


def test_pi_contract_stream_maps_to_the_normalized_kinds():
    """pi's stream normalizes, and the deltas that built each block do not.

    The fixture's 27 lines carry one thinking block, one tool call, its
    result, and one answer. Anything that emits per delta — or that emits the
    same block again from ``turn_end`` — shows up here as extra events. The
    answer's own deltas only feed its preview, under the id its block names.
    """
    runtime = PiRuntime(clock=_Clock(step=1.0))
    events = [
        event for line in _pi_contract_lines() if (event := runtime.parse_event(line)) is not None
    ]
    answer = next(event for event in events if event.event_type == "message")
    assert _pieces(events) == [
        (answer.payload["preview_id"], "PWD_OK: "),
        (answer.payload["preview_id"], "synthetic agent loop round trip complete."),
    ]
    kinds = [event.event_type for event in events if event.payload.get("protocol") != REPLY_PREVIEW]
    assert kinds == [
        "lifecycle",  # session
        "thinking",
        "tool_use",
        "usage",  # end of the assistant message that made the call
        "tool_result",
        "message",  # the answer
        "usage",
        "lifecycle",  # agent_end
    ]
    assert "raw" not in kinds, "the recorded pi format should be fully recognized"


def test_pi_answer_becomes_a_message_event():
    """The answer arrives as ``message``, which is what reaches the thread.

    ``agent_thread_messages`` is populated from ``message`` events alone, so
    this is the difference between a visible reply — and the next turn having
    any context — and a conversation that looks empty.
    """
    runtime = PiRuntime()
    events = [runtime.parse_event(line) for line in _pi_contract_lines()]
    messages = [e for e in events if e and e.event_type == "message"]
    assert len(messages) == 1
    assert messages[0].payload["text"] == "PWD_OK: synthetic agent loop round trip complete."


def test_pi_tool_call_and_result_carry_what_the_runner_reads():
    """Tool events keep the keys the runner and the UI look for."""
    runtime = PiRuntime()
    events = [runtime.parse_event(line) for line in _pi_contract_lines()]

    tool_use = next(e for e in events if e and e.event_type == "tool_use")
    assert tool_use.payload["name"] == "bash"
    assert tool_use.payload["id"] == "synthetic-tool-call-1"
    assert tool_use.payload["input"] == {"command": "pwd"}

    tool_result = next(e for e in events if e and e.event_type == "tool_result")
    assert tool_result.payload["tool_use_id"] == "synthetic-tool-call-1"
    # The runner's blocked-egress detection reads exactly these two.
    assert tool_result.payload["is_error"] is False
    assert "/workspace" in tool_result.payload["content"]


def test_pi_failed_tool_result_reports_the_error_flag():
    """A failing tool call is flagged, so the runner can count and report it."""
    line = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "toolCallId": "synthetic-tool-call-9",
                "toolName": "bash",
                "content": [{"type": "text", "text": "curl: (7) Failed to connect"}],
                "isError": True,
            },
        }
    )
    event = PiRuntime().parse_event(line)
    assert event.event_type == "tool_result"
    assert event.payload["is_error"] is True


def test_pi_usage_reports_tokens_without_claiming_a_cost():
    """Token counts are surfaced; the zero-filled cost block is not."""
    runtime = PiRuntime()
    events = [runtime.parse_event(line) for line in _pi_contract_lines()]
    usage = [e for e in events if e and e.event_type == "usage"][-1]
    assert usage.payload["input_tokens"] == 12
    assert usage.payload["output_tokens"] == 8
    assert usage.payload["cache_read_tokens"] == 4
    assert "cost" not in usage.payload
    assert runtime.capabilities().cost_report is False


def test_pi_names_each_response_so_its_gateway_request_can_be_found():
    """Every block of one response, and its usage, carry pi's ``responseId``.

    pi repeats the partial assistant message on each ``message_update``; the
    fixture keeps only its ``responseId``. Against the gateway that id is the
    chat-completion id the request is logged under, so it is what the
    inspector asks the gateway for.
    """
    runtime = PiRuntime()
    events = [e for line in _pi_contract_lines() if (e := runtime.parse_event(line))]
    assert [
        (e.event_type, e.payload.get("usage_id"))
        for e in events
        if e.event_type in {"thinking", "tool_use", "message", "usage"}
    ] == [
        ("thinking", "synthetic-response-1"),
        ("tool_use", "synthetic-response-1"),
        ("usage", "synthetic-response-1"),
        ("message", "synthetic-response-2"),
        ("usage", "synthetic-response-2"),
    ]


@pytest.mark.parametrize(
    "message", [None, {"role": "assistant"}, {"responseId": ""}, {"responseId": 7}]
)
def test_pi_without_a_response_id_claims_no_identity(message):
    """A response pi could not name stays unattributed rather than blank."""
    record: dict[str, object] = {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_end", "contentIndex": 0, "content": "hi"},
    }
    if message is not None:
        record["message"] = message
    event = PiRuntime().parse_event(json.dumps(record))
    assert event.event_type == "message"
    assert "usage_id" not in event.payload


def test_pi_unknown_block_kind_is_reported_once_not_per_token():
    """A new sub-event degrades to one diagnostic, not one row per delta."""
    runtime = PiRuntime()
    line = json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "citation_end"}})
    first = runtime.parse_event(line)
    assert first is not None and first.event_type == "raw"
    assert runtime.parse_event(line) is None
    # A fresh job starts over: the suppression is per-run state.
    assert PiRuntime().parse_event(line).event_type == "raw"


def test_pi_unknown_line_types_still_become_raw():
    """Promotion to Tier 1 keeps the never-drop-a-line contract."""
    runtime = PiRuntime()
    for line in ('{"type":"brand_new_thing","x":1}', "not json at all", "[1,2,3]"):
        event = runtime.parse_event(line)
        assert event is not None and event.event_type == "raw"
    assert runtime.parse_event("   ") is None


def test_claude_prepare_points_at_the_gateway_and_disables_telemetry():
    """The agent talks to our gateway with the job's token and phones nobody."""
    argv, env = ClaudeCodeRuntime().prepare(
        workdir="/tmp/x",
        task_prompt="do the thing",
        model="glm-5.1",
        gateway_base_url="https://gateway.example.com/v1",
        credential="ajt.a.b",
    )
    assert argv[:3] == ["claude", "-p", "do the thing"]
    assert "--output-format" in argv and "stream-json" in argv
    # Ignore both user-level and committed .mcp.json servers. Platform MCP is
    # supplied explicitly later; the effective set here must stay empty.
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
    assert env["ANTHROPIC_API_KEY"] == "ajt.a.b"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"


@pytest.mark.parametrize("live", [False, True])
def test_claude_native_requests_use_bounded_extended_retries(live):
    runtime = ClaudeCodeRuntime()
    settings = {
        "workdir": "/w",
        "model": "kimi-k3",
        "gateway_base_url": "http://gateway",
        "credential": "inference-credential",
    }
    _argv, env = (
        runtime.prepare_live(**settings)
        if live
        else runtime.prepare(task_prompt="do it", **settings)
    )
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "15"
    assert env["CLAUDE_CODE_RETRY_WATCHDOG"] == "0"


def test_codex_prepare_and_config_target_the_gateway():
    """Codex is pointed at the gateway through a scratch provider config."""
    runtime = CodexRuntime()
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url="http://localhost:8000",
        credential="ajt.a.b",
    )
    assert argv[:2] == ["codex", "exec"]
    assert env["CODEX_API_KEY"] == "ajt.a.b"

    # The owner's model must actually reach the CLI. This used to ride
    # `CODEX_MODEL`, which Codex does not read, so the choice was dropped
    # silently and the run used whatever the CLI defaulted to.
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "glm-5.1"

    joined = " ".join(argv)
    assert 'model_provider="hybridinference"' in joined
    assert 'model_providers.hybridinference.name="HybridInference"' in joined
    assert 'base_url="http://localhost:8000/v1"' in joined
    # Codex removed chat wire support upstream (openai/codex#7782); the gateway
    # serves /v1/responses. `chat` here would fail every turn.
    assert 'wire_api="responses"' in joined
    # The operator's own Codex config must not reach a sandbox run.
    assert "--ignore-user-config" in argv
    # The prompt is the final operand, after every flag.
    assert argv[-1] == "do it"


def test_codex_sandbox_mode_follows_the_backend_boundary():
    """Only an outer sandbox may replace Codex's own workspace sandbox."""
    runtime = CodexRuntime()
    kwargs = {
        "workdir": "/tmp/x",
        "task_prompt": "do it",
        "model": "glm-5.1",
        "gateway_base_url": "http://localhost:8000",
        "credential": "ajt.a.b",
    }

    process_argv, _ = runtime.prepare(**kwargs)
    container_argv, _ = runtime.prepare(**kwargs, provides_isolation=True)

    assert process_argv[process_argv.index("--sandbox") + 1] == "workspace-write"
    assert container_argv[container_argv.index("--sandbox") + 1] == "danger-full-access"


def test_codex_events_normalize():
    """Codex item events map onto the same normalized kinds."""
    runtime = CodexRuntime()
    tool = runtime.parse_event(
        json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
    )
    assert tool.event_type == "tool_use"
    result = runtime.parse_event(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "command_execution",
                    "command": "ls",
                    "aggregated_output": "README.md\n",
                    "exit_code": 0,
                },
            }
        )
    )
    assert result.event_type == "tool_result"
    assert result.payload == {
        "tool_use_id": "item-1",
        "is_error": False,
        "content": "README.md\n",
        "exit_code": 0,
    }
    message = runtime.parse_event(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
    )
    assert message.event_type == "message"
    assert runtime.parse_event(json.dumps({"type": "turn.completed"})).event_type == "lifecycle"


def test_codex_native_session_id_is_captured_and_resumed_from_state_root():
    runtime = CodexRuntime()
    started = runtime.parse_event(
        json.dumps({"type": "thread.started", "thread_id": "codex-thread-123"})
    )
    assert started.event_type == "lifecycle"
    assert runtime.session_id == "codex-thread-123"

    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="only the follow-up",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
        provides_isolation=True,
        session_state_dir="/agent-state",
        resume_session_id="codex-thread-123",
    )
    resume_at = argv.index("resume")
    assert argv[resume_at : resume_at + 3] == [
        "resume",
        "codex-thread-123",
        "only the follow-up",
    ]
    assert env["CODEX_HOME"] == "/agent-state"


def test_generic_runtime_streams_raw_and_never_uses_a_shell():
    """Tier 2 gets a log tail for free, with no shell interpretation."""
    runtime = GenericRuntime("mytool --prompt {prompt} --model {model}")
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="a b; rm -rf /",
        model="m",
        gateway_base_url="http://gw",
        credential="ajt.a.b",
    )
    # The dangerous string stays a single argv element — no shell, no split.
    assert "a b; rm -rf /" in argv
    assert env["OPENAI_API_KEY"] == "ajt.a.b"
    assert runtime.parse_event("some log line").event_type == "raw"


def test_registry_resolution_and_refusal():
    """Only runtimes with a registered live-session transport resolve."""
    assert isinstance(get_runtime("claude-code"), ClaudeCodeRuntime)
    assert isinstance(get_runtime("codex"), CodexRuntime)
    assert isinstance(get_runtime("pi"), PiRuntime)
    assert isinstance(get_runtime("opencode"), OpencodeRuntime)
    assert isinstance(get_runtime("kilo"), KiloRuntime)
    with pytest.raises(KeyError):
        get_runtime("does-not-exist")


def test_pi_runtime_targets_the_gateway_through_its_wrapper():
    """pi is invoked via the wrapper, with everything the wrapper needs.

    pi ignores OPENAI_BASE_URL (verified against a local fake: zero hits, a
    real OpenAI 401), so the invocation must go through `pi-freeinference`,
    which writes the provider config from the environment. A regression here
    silently sends jobs to api.openai.com, where the egress policy turns them
    into timeouts that read like a broken model.
    """
    runtime = PiRuntime()
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="do the thing; carefully",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
    )

    assert argv[0] == "pi-freeinference"
    assert runtime.binary == "pi-freeinference"
    # The prompt stays one argv element — no shell anywhere on the path.
    assert "do the thing; carefully" in argv
    assert argv[argv.index("--model") + 1] == "glm-5.1"
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    assert "--no-session" not in argv
    # The pinned pi release implements MCP through executable extensions,
    # including extensions committed under .pi/.
    assert "--no-extensions" in argv
    # Everything the wrapper reads to build ~/.pi/agent/models.json.
    assert env["OPENAI_BASE_URL"] == "http://backend:8080/v1"
    assert env["OPENAI_API_KEY"] == "ajt.a.b"
    assert env["PI_GATEWAY_MODEL"] == "glm-5.1"
    # Tier 1 since the recorded staging runs pinned the format.
    assert runtime.capabilities().tier == 1
    assert runtime.capabilities().normalized_events is True


def test_pi_native_session_is_persisted_and_resumed_explicitly():
    runtime = PiRuntime()
    event = runtime.parse_event(
        json.dumps({"type": "session", "id": "pi-session-123", "version": 3})
    )
    assert event.payload["session_id"] == "pi-session-123"
    assert runtime.session_id == "pi-session-123"

    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="only the follow-up",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
        session_state_dir="/agent-state",
        resume_session_id="pi-session-123",
    )
    assert argv[argv.index("--session-dir") + 1] == "/agent-state"
    assert argv[argv.index("--session") + 1] == "pi-session-123"
    assert env["PI_CODING_AGENT_DIR"] == "/run/agent-activation/pi"


@pytest.mark.parametrize(
    "runtime",
    [
        CodexRuntime(),
        PiRuntime(),
        OpencodeRuntime(),
        KiloRuntime(),
        GenericRuntime("some-agent {prompt}"),
    ],
    ids=["codex", "pi", "opencode", "kilo", "generic"],
)
def test_mcp_config_fails_closed_without_an_isolated_relay(runtime):
    """An adapter with no mediated path refuses rather than dropping servers.

    ``ClaudeCodeRuntime`` is excluded because it now *has* that path; the
    invariant it has to satisfy instead is the next test's.
    """
    config = RuntimeMCPConfig(server_ids=("repository-supplied-server",))

    with pytest.raises(RuntimeMCPUnavailableError) as raised:
        runtime.prepare(
            workdir="/tmp/x",
            task_prompt="do it",
            model="glm-5.1",
            gateway_base_url="http://backend:8080",
            credential="mcp-secret-must-not-leak",
            mcp_config=config,
        )

    # Server ids and credentials do not enter an error that may reach job logs.
    assert "repository-supplied-server" not in str(raised.value)
    assert "mcp-secret-must-not-leak" not in str(raised.value)


def test_a_brokered_runtime_emits_only_relay_endpoints_and_no_credential():
    """The same rule, for the adapter that does resolve servers.

    "Fails closed" was only ever a stand-in for this: a requested server must
    not become direct MCP, and neither credential must become a CLI argument.

    **Two addresses and two credentials.** MCP is the cloud agent's relay, not
    the inference gateway — a sandbox pointed at the gateway for tools reaches
    a service that no longer answers for them. This case is where the two are
    kept apart, so it asserts that the inference credential is *not* what opens
    MCP.
    """
    argv, env = ClaudeCodeRuntime().prepare(
        workdir="/tmp/x",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="agr-inference-grant-must-not-leak",
        mcp_config=RuntimeMCPConfig(
            server_ids=("github",),
            base_url="http://agent-mcp:8000",
            credential="mcp-token-must-not-leak",
        ),
    )
    rendered = " ".join(argv)

    assert "mcp-token-must-not-leak" not in rendered
    assert "agr-inference-grant-must-not-leak" not in rendered
    assert env["AGENT_MCP_TOKEN"] == "mcp-token-must-not-leak"
    assert env["ANTHROPIC_API_KEY"] == "agr-inference-grant-must-not-leak"
    assert env["AGENT_MCP_TOKEN"] != env["ANTHROPIC_API_KEY"]
    servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    assert servers[MERGED_MCP_SERVER_NAME]["url"] == "http://agent-mcp:8000/v1/agent/mcp"
    assert "--strict-mcp-config" in argv


@pytest.mark.parametrize(
    "runtime",
    [ClaudeCodeRuntime(), CodexRuntime(), PiRuntime(), OpencodeRuntime(), KiloRuntime()],
    ids=["claude", "codex", "pi", "opencode", "kilo"],
)
def test_runtime_credentials_stay_out_of_process_arguments(runtime):
    """The model-scoped token is environment-only, never argv/log material."""
    credential = "ajt.secret.value"
    argv, _env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential=credential,
        mcp_config=RuntimeMCPConfig(),
    )

    assert all(credential not in argument for argument in argv)


def test_opencode_runtime_targets_the_gateway_through_its_wrapper():
    """OpenCode is invoked via the wrapper, with everything it needs.

    OpenCode ignores OPENAI_BASE_URL and its built-in openai provider speaks
    the Responses API; the wrapper declares a chat-completions provider over
    the SDK package bundled in the binary and disables the models.dev fetch
    that otherwise hard-fails every offline run.
    """
    runtime = OpencodeRuntime()
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="fix the bug; then run tests",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
    )

    assert argv[0] == "opencode-freeinference"
    assert runtime.binary == "opencode-freeinference"
    assert "fix the bug; then run tests" in argv
    # The model rides inside the provider-qualified -m argument.
    assert argv[argv.index("-m") + 1] == "freeinference/glm-5.1"
    # The sandbox is the boundary; an interactive permission gate inside it
    # only guarantees the agent cannot do the work.
    assert "--auto" in argv
    assert env["OPENAI_BASE_URL"] == "http://backend:8080/v1"
    assert env["OPENAI_API_KEY"] == "ajt.a.b"
    assert env["OPENCODE_GATEWAY_MODEL"] == "glm-5.1"
    assert runtime.parse_event('{"type":"text"}').event_type == "raw"
    assert runtime.capabilities().tier == 2


def test_opencode_native_session_id_and_xdg_state_are_explicit():
    runtime = OpencodeRuntime()
    runtime.parse_event('{"type":"text","sessionID":"ses_open_123"}')
    assert runtime.session_id == "ses_open_123"
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="only the follow-up",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
        session_state_dir="/agent-state",
        resume_session_id="ses_open_123",
    )
    assert argv[argv.index("--session") + 1] == "ses_open_123"
    assert env["XDG_DATA_HOME"] == "/agent-state/opencode/data"
    assert env["OPENCODE_FREEINFERENCE_CONFIG_DIR"] == "/run/agent-activation/opencode"


def test_kilo_runtime_targets_the_gateway_through_its_wrapper():
    """Kilo is invoked via the wrapper, with everything it needs.

    Kilo's CLI is an OpenCode fork and inherits both gaps: OPENAI_BASE_URL is
    ignored, and startup fetches the models.dev catalog. The wrapper declares
    a chat-completions provider over the SDK package bundled in the binary,
    disables that fetch, and additionally kills the fork's own phone-home
    paths (PostHog, session ingest and share links to app.kilo.ai).
    """
    runtime = KiloRuntime()
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="fix the bug; then run tests",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
    )

    assert argv[0] == "kilo-freeinference"
    assert runtime.binary == "kilo-freeinference"
    assert "fix the bug; then run tests" in argv
    # The model rides inside the provider-qualified -m argument.
    assert argv[argv.index("-m") + 1] == "freeinference/glm-5.1"
    # The sandbox is the boundary; an interactive permission gate inside it
    # only guarantees the agent cannot do the work.
    assert "--auto" in argv
    assert env["OPENAI_BASE_URL"] == "http://backend:8080/v1"
    assert env["OPENAI_API_KEY"] == "ajt.a.b"
    assert env["KILO_GATEWAY_MODEL"] == "glm-5.1"
    assert runtime.parse_event('{"type":"text"}').event_type == "raw"
    assert runtime.capabilities().tier == 2


def test_kilo_native_session_id_and_xdg_state_are_explicit():
    runtime = KiloRuntime()
    runtime.parse_event('{"type":"text","sessionID":"ses_kilo_123"}')
    assert runtime.session_id == "ses_kilo_123"
    argv, env = runtime.prepare(
        workdir="/tmp/x",
        task_prompt="only the follow-up",
        model="glm-5.1",
        gateway_base_url="http://backend:8080",
        credential="ajt.a.b",
        session_state_dir="/agent-state",
        resume_session_id="ses_kilo_123",
    )
    assert argv[argv.index("--session") + 1] == "ses_kilo_123"
    assert env["XDG_DATA_HOME"] == "/agent-state/kilo/data"
    assert env["KILO_FREEINFERENCE_CONFIG_DIR"] == "/run/agent-activation/kilo"


def test_capabilities_declare_tiers():
    """Tiering is explicit so the platform never assumes support."""
    assert ClaudeCodeRuntime().capabilities().tier == 1
    assert GenericRuntime("x").capabilities().tier == 2
    assert GenericRuntime("x").capabilities().normalized_events is False


def test_system_telemetry_does_not_become_408_milestones():
    """A progress counter must not enter the event log once per emission.

    Shape taken from the first real staging job, not from documentation: it
    stored 412 lifecycle events, 408 of them ``subtype: "thinking_tokens"``,
    which the UI then drew as 408 ticked-off steps. The first occurrence is
    kept as a raw diagnostic so a new subtype stays discoverable; the rest are
    dropped.
    """
    runtime = ClaudeCodeRuntime()
    line = json.dumps({"type": "system", "subtype": "thinking_tokens", "session_id": "s1"})

    first = runtime.parse_event(line)
    assert first is not None
    assert first.event_type == "raw", "an unclassified subtype must not pass as a milestone"
    assert "thinking_tokens" in first.payload["reason"]

    for _ in range(407):
        assert runtime.parse_event(line) is None, "repeats must be suppressed, not stored"


def test_real_milestones_still_arrive_as_lifecycle():
    """The allowlisted subtypes keep their phase, model and runtime version."""
    runtime = ClaudeCodeRuntime()
    event = runtime.parse_event(
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "model": "glm-5.1",
                "claude_code_version": "2.1.220",
            }
        )
    )
    assert event is not None
    assert event.event_type == "lifecycle"
    assert event.payload["phase"] == "init"
    assert event.payload["runtime_version"] == "2.1.220"

    compacted = runtime.parse_event(json.dumps({"type": "system", "subtype": "compact_boundary"}))
    assert compacted is not None and compacted.event_type == "lifecycle"


def _init(payload: dict[str, object]) -> dict[str, object]:
    """Parse one synthetic init event and return the payload we stored."""
    runtime = ClaudeCodeRuntime()
    event = runtime.parse_event(json.dumps({"type": "system", "subtype": "init", **payload}))
    assert event is not None and event.event_type == "lifecycle"
    return event.payload


def test_init_republishes_the_harness_the_cli_reported():
    """The CLI's own statement of what it loaded survives normalization.

    Claude Code never puts its system prompt on the wire, so the tool menu and
    the permission mode it reports at init are the only evidence the platform
    has of what the session actually started with.
    """
    payload = _init(
        {
            "session_id": "sess-1",
            "model": "glm-5.1",
            "claude_code_version": "2.1.220",
            "permissionMode": "bypassPermissions",
            "tools": ["Bash", "Read", "Read", "mcp__relay__search"],
            "mcp_servers": [{"name": "relay", "status": "connected"}],
        }
    )
    assert payload["harness"] == {
        "tools": ["Bash", "Read", "mcp__relay__search"],
        "mcp_servers": [{"name": "relay", "status": "connected"}],
        "permission_mode": "bypassPermissions",
    }


def test_init_without_a_harness_report_carries_none():
    """A runtime that says nothing about itself gets no empty inventory."""
    assert "harness" not in _init({"model": "glm-5.1"})
    assert "harness" not in _init({"tools": [], "mcp_servers": [], "permissionMode": "  "})


def test_subagent_init_is_not_the_sessions_inventory():
    """A subagent emits init too, for a narrower menu, and is not the session.

    The normalized event carries no marker distinguishing the two, so
    republishing a subagent's menu would read as the session's own.
    """
    payload = _init(
        {
            "parent_tool_use_id": "toolu_1",
            "permissionMode": "bypassPermissions",
            "tools": ["Read"],
        }
    )
    assert "harness" not in payload


def test_harness_inventory_keeps_only_what_it_can_attribute():
    """Entries the CLI did not send in the stated shape name nothing."""
    payload = _init(
        {
            "tools": ["Bash", 7, None, {"name": "Read"}, "   "],
            "mcp_servers": [{"status": "connected"}, "relay", {"name": "ok"}],
            "permissionMode": 7,
        }
    )
    assert payload["harness"] == {
        "tools": ["Bash"],
        "mcp_servers": [{"name": "ok", "status": None}],
    }


def test_harness_tools_are_bounded_and_say_so():
    """The bound is reported, so a short list is never a short menu."""
    over = _init({"tools": [f"tool-{index}" for index in range(MAX_INIT_TOOLS + 1)]})["harness"]
    assert isinstance(over, dict)
    assert len(over["tools"]) == MAX_INIT_TOOLS
    assert over["tools_truncated"] is True

    exact = _init({"tools": [f"tool-{index}" for index in range(MAX_INIT_TOOLS)]})["harness"]
    assert isinstance(exact, dict)
    assert len(exact["tools"]) == MAX_INIT_TOOLS
    assert "tools_truncated" not in exact


def test_the_contract_fixture_carries_the_harness_fields():
    """The synthetic stream is pinned to the shape the CLI was observed to emit."""
    runtime = ClaudeCodeRuntime()
    events = [event for line in _contract_lines() if (event := runtime.parse_event(line))]
    init = next(event for event in events if event.payload.get("phase") == "init")
    assert init.payload["harness"]["tools"] == ["bash", "read"]
    assert init.payload["harness"]["permission_mode"] == "default"


def test_suppression_does_not_leak_between_jobs():
    """Each job gets its own adapter, so its first occurrence is still reported."""
    line = json.dumps({"type": "system", "subtype": "thinking_tokens"})
    assert ClaudeCodeRuntime().parse_event(line) is not None
    assert ClaudeCodeRuntime().parse_event(line) is not None


# ── MCP wiring ported from the gateway at H4 ──────────────────────────
#
# These three lived in the gateway's ``test_agent_mcp.py``. The registry and
# proxy filters were subsequently moved here too; this note records why the
# runtime-specific cases remain in the adapter suite. A sweep at H4 found these
# three pin properties nothing here covered: the flag that shuts
# out a repository's own servers when a job requests none, the one-normalisation
# of the gateway base url, and which runtimes may claim MCP at all. The other
# three cases in that section were already covered here under different names
# (``test_a_brokered_runtime_emits_only_relay_endpoints_and_no_credential``,
# ``test_runtime_credentials_stay_out_of_process_arguments``,
# ``test_mcp_config_fails_closed_without_an_isolated_relay``) and were not
# duplicated.

_MCP_GATEWAY = "http://backend:8080"


def _mcp_argv(*server_ids: str) -> list[str]:
    """Build the Claude Code invocation for a job with these servers."""
    argv, _env = ClaudeCodeRuntime().prepare(
        workdir="/tmp/x",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url=_MCP_GATEWAY,
        credential="mcp-secret-must-not-leak",
        mcp_config=RuntimeMCPConfig(server_ids=server_ids),
    )
    return argv


def _mcp_config_of(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--mcp-config") + 1])


def test_repository_supplied_mcp_config_is_shut_out_even_with_no_servers():
    """The repository is untrusted input and may not add its own servers.

    Reproduced against the real CLI in a job's exact configuration (headless,
    ``bypassPermissions``, cwd = a checkout carrying a ``.mcp.json``): without
    this flag the CLI reports the repository's server alongside the platform's,
    with no approval step — the permission mode the sandbox needs for its own
    work is what removes the gate. Hence unconditional, rather than paired with
    having servers: a job with no MCP at all is where an injected one would be
    least expected.
    """
    argv = _mcp_argv()
    assert "--strict-mcp-config" in argv
    assert _mcp_config_of(argv) == {"mcpServers": {}}


def test_the_relay_base_url_is_normalized_once():
    """A relay url already ending in /v1 must not produce /v1/v1."""
    argv, _env = ClaudeCodeRuntime().prepare(
        workdir="/tmp/x",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url=_MCP_GATEWAY,
        credential="inference-credential",
        mcp_config=RuntimeMCPConfig(
            server_ids=("github",),
            base_url=f"{_MCP_GATEWAY}/v1",
            credential="mcp-token",
        ),
    )
    servers = _mcp_config_of(argv)["mcpServers"]
    assert servers[MERGED_MCP_SERVER_NAME]["url"] == f"{_MCP_GATEWAY}/v1/agent/mcp"


def test_requesting_mcp_with_no_relay_address_refuses_rather_than_guessing():
    """**The failure this separation makes possible, refused explicitly.**

    Falling back to the inference address would point the sandbox at a service
    that does not answer for MCP — 404s from an unrelated host, which reads as
    "the tool is broken" rather than "this run has no relay".
    """
    with pytest.raises(RuntimeMCPUnavailableError):
        ClaudeCodeRuntime().prepare(
            workdir="/tmp/x",
            task_prompt="do it",
            model="glm-5.1",
            gateway_base_url=_MCP_GATEWAY,
            credential="inference-credential",
            mcp_config=RuntimeMCPConfig(server_ids=("github",)),
        )


def test_only_runtimes_that_can_also_shut_out_repo_config_report_mcp():
    """The claim is "both halves", so a runtime that has only one does not make it.

    A runtime appears here only when it can be pointed at the platform's relay
    *and* be stopped from reading a repository's own MCP config. Codex and pi
    fail the second half at their pinned versions and say so, rather than
    running with dropped tools or somebody else's servers.
    """
    assert runtimes_supporting_mcp() == ["claude-code", "codex", "kilo", "opencode", "pi"]
    assert ClaudeCodeRuntime().capabilities().mcp is True
    assert OpencodeRuntime().capabilities().mcp is True
    assert PiRuntime().capabilities().mcp is True
    assert CodexRuntime().capabilities().mcp is True
    assert KiloRuntime().capabilities().mcp is True


def test_pi_loads_the_images_bridge_and_nothing_else():
    """The only extension pi may load is the one the image was built with.

    pi has no MCP client, so the bridge is the platform's MCP path on this
    runtime -- and, because a project-local extension would otherwise be loaded
    from the checkout, `--no-extensions` has to be in the same argv as the
    explicit `-e`. Verified against 0.73.1: with both flags a repository's
    `.pi/extensions/*.ts` does not load and the bridge's tools do.
    """
    argv, env = PiRuntime().prepare(
        workdir="/w",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url=_MCP_GATEWAY,
        credential="inference-credential",
        mcp_config=RuntimeMCPConfig(
            server_ids=("github",),
            base_url=_MCP_GATEWAY,
            credential="mcp-token",
        ),
    )
    assert "--no-extensions" in argv
    assert argv[argv.index("-e") + 1].endswith("pi-mcp-bridge.ts")
    assert json.loads(env["AGENT_MCP_SERVERS"]) == {
        MERGED_MCP_SERVER_NAME: f"{_MCP_GATEWAY}/v1/agent/mcp"
    }
    assert env["AGENT_MCP_TOKEN"] == "mcp-token"
    assert "mcp-token" not in " ".join(argv)


@pytest.mark.parametrize("runtime", [CodexRuntime, KiloRuntime])
@pytest.mark.parametrize("live", [False, True])
def test_codex_and_kilo_carry_the_same_mcp_scope_in_both_transports(runtime, live):
    """Fresh and resident invocations share one relay and a separate MCP token."""
    agent = runtime()
    settings = {
        "workdir": "/w",
        "model": "glm-5.1",
        "gateway_base_url": _MCP_GATEWAY,
        "credential": "inference-token",
        "mcp_config": RuntimeMCPConfig(
            server_ids=("github", "docs"),
            base_url=_MCP_GATEWAY,
            credential="mcp-token",
        ),
    }
    argv, env = (
        agent.prepare_live(**settings) if live else agent.prepare(task_prompt="do it", **settings)
    )
    assert env["AGENT_MCP_TOKEN"] == "mcp-token"
    assert "mcp-token" not in " ".join(argv)
    if runtime is CodexRuntime:
        import tomllib

        config = tomllib.loads(next(arg for arg in argv if arg.startswith("mcp_servers =")))
        assert set(config["mcp_servers"]) == {"session"}
        assert config["mcp_servers"]["session"]["bearer_token_env_var"] == "AGENT_MCP_TOKEN"
        assert config["mcp_servers"]["session"]["url"] == _MCP_GATEWAY + "/v1/agent/mcp"
    else:
        assert json.loads(env["AGENT_MCP_SERVERS"]) == {"session": _MCP_GATEWAY + "/v1/agent/mcp"}


def test_a_wrapper_runtime_publishes_the_relay_urls_and_the_token_reference():
    """The Python half: address here, the dialect reference in the wrapper.

    How a CLI spells "read this from the environment" is not portable -- Claude
    Code expands ``${VAR}``, the OpenCode family expands ``{env:VAR}`` -- so the
    endpoint map is neutral and each wrapper adds the header in its own syntax,
    next to the pinned binary that has to expand it.

    The run names two connections here and still gets one entry: the relay's
    bare prefix answers for all of them and namespaces what it lists by alias,
    so one entry per connection would register the same catalogue twice.
    """
    argv, env = OpencodeRuntime().prepare(
        workdir="/w",
        task_prompt="do it",
        model="glm-5.1",
        gateway_base_url=_MCP_GATEWAY,
        credential="inference-credential",
        mcp_config=RuntimeMCPConfig(
            server_ids=("github", "docs"),
            base_url=f"{_MCP_GATEWAY}/v1",
            credential="mcp-token-must-not-leak",
        ),
    )
    assert json.loads(env["AGENT_MCP_SERVERS"]) == {
        MERGED_MCP_SERVER_NAME: f"{_MCP_GATEWAY}/v1/agent/mcp"
    }
    # The token travels in the environment the wrapper interpolates from, never
    # in argv, and never in the endpoint map that becomes a config file.
    assert env["AGENT_MCP_TOKEN"] == "mcp-token-must-not-leak"
    assert "mcp-token-must-not-leak" not in " ".join(argv)
    assert "mcp-token-must-not-leak" not in env["AGENT_MCP_SERVERS"]


def test_the_resident_path_carries_the_same_relay_config():
    """A hot session must not be the turn where MCP quietly disappears.

    ``prepare_live`` assembles its own argv/env around the same dialect layer,
    so a change that only touched ``prepare`` would leave every resumed
    conversation without tools -- and the agent would work around their absence
    instead of failing.
    """
    argv, env = OpencodeRuntime().prepare_live(
        workdir="/w",
        model="glm-5.1",
        gateway_base_url=_MCP_GATEWAY,
        credential="inference-credential",
        mcp_config=RuntimeMCPConfig(
            server_ids=("github",),
            base_url=_MCP_GATEWAY,
            credential="mcp-token",
        ),
    )
    assert json.loads(env["AGENT_MCP_SERVERS"]) == {
        MERGED_MCP_SERVER_NAME: f"{_MCP_GATEWAY}/v1/agent/mcp"
    }
    assert env["AGENT_MCP_TOKEN"] == "mcp-token"
    assert argv[0] == "http-agent-live-bridge"


def test_a_wrapper_runtime_refuses_connections_it_has_no_credential_for():
    """Referenced, not embedded, means a missing token is an empty bearer.

    Left to run, every call would be refused with a 401 that reads as a broken
    server rather than as a run that was never given a credential.
    """
    with pytest.raises(RuntimeMCPUnavailableError):
        OpencodeRuntime().prepare(
            workdir="/w",
            task_prompt="do it",
            model="glm-5.1",
            gateway_base_url=_MCP_GATEWAY,
            credential="inference-credential",
            mcp_config=RuntimeMCPConfig(server_ids=("github",), base_url=_MCP_GATEWAY),
        )


def _prepare(runtime, **kwargs):
    return runtime.prepare(
        workdir="/w",
        task_prompt="do it",
        model="glm-5.3",
        gateway_base_url="https://gw.test/v1",
        credential="tok",
        **kwargs,
    )


def test_claude_code_puts_the_effort_on_the_argv():
    argv, _env = _prepare(ClaudeCodeRuntime(), reasoning_effort="max")

    assert argv[argv.index("--effort") + 1] == "max"


def test_auto_adds_no_flag():
    """The whole compatibility story: an unset effort invokes exactly as before."""
    argv, _env = _prepare(ClaudeCodeRuntime())

    assert "--effort" not in argv


def test_claude_code_subagents_use_the_concrete_granted_model():
    """An `inherit` sentinel leaves the subagent's own model choice in force."""
    _argv, env = _prepare(ClaudeCodeRuntime())

    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "glm-5.3"


def test_claude_code_offers_xhigh():
    """Captured from 2.1.220. The first declaration read --help and missed it."""
    argv, _env = _prepare(ClaudeCodeRuntime(), reasoning_effort="xhigh")

    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_a_level_the_cli_does_not_have_is_refused_not_passed_through():
    """Fail closed, and here that is load-bearing rather than tidy.

    claude-code does not reject an unknown level — `--effort ultra`, `none`
    and `minimal` all arrive on the wire as `high`. Forwarding one would run
    the turn at a depth nobody chose while the UI reported the one they did.
    """
    for level in ("ultra", "none", "minimal"):
        with pytest.raises(RuntimeEffortUnsupportedError):
            _prepare(ClaudeCodeRuntime(), reasoning_effort=level)


def test_codex_expresses_effort_as_a_config_override():
    """Verified against the pinned 0.145.0, not assumed — see the wire fixture.

    The CLI turns this into ``reasoning.effort`` in its /v1/responses body,
    which is the field the gateway's Responses translation already reads.
    """
    argv, _env = _prepare(CodexRuntime(), reasoning_effort="high")

    at = argv.index('model_reasoning_effort="high"')
    assert argv[at - 1] == "-c"


def test_codex_auto_adds_no_override():
    argv, _env = _prepare(CodexRuntime())

    assert not any(part.startswith("model_reasoning_effort") for part in argv)


def test_pi_needs_both_the_flag_and_the_provider_declaration():
    """`--thinking` alone is a no-op — verified against 0.73.1, not assumed.

    Without the provider entry declaring the model as a reasoning model, pi
    emits no ``reasoning_effort`` whatsoever, so the flag would look set and
    change nothing. The wrapper writes that entry, hence the environment half.
    """
    argv, env = _prepare(PiRuntime(), reasoning_effort="low")

    assert argv[argv.index("--thinking") + 1] == "low"
    assert env["PI_GATEWAY_REASONING"] == "1"


def test_pi_auto_leaves_the_provider_entry_alone():
    """Declaring the model as reasoning would make pi send `medium` always."""
    argv, env = _prepare(PiRuntime())

    assert "--thinking" not in argv
    assert "PI_GATEWAY_REASONING" not in env


def test_pi_does_not_offer_the_levels_that_do_not_survive():
    """`xhigh` reaches the wire as `high`, so offering it would be a lie."""
    for level in ("xhigh", "off"):
        with pytest.raises(RuntimeEffortUnsupportedError):
            _prepare(PiRuntime(), reasoning_effort=level)


def test_config_file_runtimes_carry_the_level_in_the_environment():
    """opencode and kilo have no flag for this; their wrapper's config does."""
    for runtime, var in (
        (OpencodeRuntime(), "OPENCODE_GATEWAY_REASONING_EFFORT"),
        (KiloRuntime(), "KILO_GATEWAY_REASONING_EFFORT"),
    ):
        argv, env = _prepare(runtime, reasoning_effort="high")
        assert env[var] == "high"
        # Nothing lands on the command line, so a wrapper that ignored the
        # variable would produce a turn with no effort rather than a broken one.
        assert not any("reasoning" in part.lower() for part in argv)

        _argv, auto_env = _prepare(runtime)
        assert var not in auto_env


def test_an_unknown_runtime_still_refuses_rather_than_ignores():
    """The base class stays fail-closed for whatever gets registered next."""

    class Newcomer(GenericRuntime):
        name = "newcomer"

    with pytest.raises(RuntimeEffortUnsupportedError):
        _prepare(Newcomer("newcomer {prompt}"), reasoning_effort="high")


def test_the_live_invocation_carries_the_same_effort_as_the_one_shot():
    """Built by the same code, so HOT and COLD cannot drift apart."""
    runtime = ClaudeCodeRuntime()
    one_shot, _ = _prepare(runtime, reasoning_effort="low")
    live, _ = runtime.prepare_live(
        workdir="/w",
        model="glm-5.3",
        gateway_base_url="https://gw.test/v1",
        credential="tok",
        reasoning_effort="low",
    )

    assert one_shot[one_shot.index("--effort") + 1] == "low"
    assert live[live.index("--effort") + 1] == "low"


def test_only_runtimes_with_a_verified_flag_are_advertised():
    """Absent, not empty-listed: the composer reads absence as "no control"."""
    advertised = runtime_reasoning_efforts()

    # Every registered runtime was captured against the pinned CLI; see
    # tests/fixtures/agent_runtime_streams/effort_wire_shapes.md.
    assert set(advertised) == {"claude-code", "codex", "pi", "opencode", "kilo"}
    assert advertised["claude-code"] == ["low", "medium", "high", "xhigh", "max"]
    # pi is the short list, and deliberately: two of its six flag levels do not
    # survive the trip to the wire.
    assert advertised["pi"] == ["minimal", "low", "medium", "high"]


#: Increasing effort, as both CLI vocabularies spell it. Not a platform scale —
#: nothing consults this at run time. It exists so the ordering contract on
#: `reasoning_efforts` is checked here rather than trusted.
_EFFORT_RANK = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def test_every_declaration_is_ordered_weakest_to_strongest():
    """The composer takes the last level as the deepest, so order is a contract.

    A declaration written out of order would silently make the composer
    preselect the wrong depth — cheap where it should be deep, or the reverse
    on someone's bill.
    """
    for name, levels in runtime_reasoning_efforts().items():
        ranks = [_EFFORT_RANK.index(level) for level in levels]
        assert ranks == sorted(ranks), f"{name} declares {levels} out of order"
        assert len(set(ranks)) == len(ranks), f"{name} declares a level twice"
