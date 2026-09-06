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
    ClaudeCodeRuntime,
    CodexRuntime,
    GenericRuntime,
    KiloRuntime,
    OpencodeRuntime,
    PiRuntime,
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


def _pi_contract_lines() -> list[str]:
    """Return the sanitized pi ``--mode json`` stream, line by line."""
    return (_FIXTURES / "pi_stream_contract.jsonl").read_text().splitlines()


def test_pi_contract_stream_maps_to_the_normalized_kinds():
    """pi's stream normalizes, and the deltas that built each block do not.

    The fixture's 27 lines carry one thinking block, one tool call, its
    result, and one answer. Anything that emits per delta — or that emits the
    same block again from ``turn_end`` — shows up here as extra events.
    """
    runtime = PiRuntime()
    kinds = [
        event.event_type
        for line in _pi_contract_lines()
        if (event := runtime.parse_event(line)) is not None
    ]
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
    # supplied explicitly later; P0's effective set must stay empty.
    assert "--strict-mcp-config" in argv
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example.com"
    assert env["ANTHROPIC_API_KEY"] == "ajt.a.b"
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"


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
    assert servers["github"]["url"] == "http://agent-mcp:8000/v1/agent/mcp/github"
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
    assert servers["github"]["url"] == f"{_MCP_GATEWAY}/v1/agent/mcp/github"


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
    """Codex is not wired up, and says so rather than dropping tools."""
    assert runtimes_supporting_mcp() == ["claude-code"]
    assert ClaudeCodeRuntime().capabilities().mcp is True
    assert CodexRuntime().capabilities().mcp is False


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
