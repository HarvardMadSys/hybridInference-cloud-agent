"""Claude Code on an owner's Anthropic Messages connection: their URL, their key, nothing else.

The CLI's SDK appends ``/v1/messages`` to ``ANTHROPIC_BASE_URL`` (observed
against the CLI: ``/v1/messages?beta=true``, with ``x-api-key`` and
``anthropic-version: 2023-06-01``), so one trailing ``/v1`` is dropped and any
other path is kept. The check posts to the same URL. The key reaches the CLI
only through its environment, never an argument, and never under an OpenAI
name; an OpenAI-style adapter refuses a Messages connection outright.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from cloud_agent.application.model_connection_anthropic import messages_url, models_url
from cloud_agent_host.resident_launch_config import SharedResidentLaunchConfig, resident_launch
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransport
from cloud_agent_host.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    ExactBaseUrl,
    KiloRuntime,
    MessagesBaseUrl,
    OpencodeRuntime,
    PiRuntime,
    RuntimeConnectionUnsupportedError,
    RuntimeMCPConfig,
    connection_base_url,
)
from cloud_agent_host.runtimes.base import MCP_TOKEN_ENV
from cloud_agent_shared.model_connections import ModelConnectionAccess, anthropic_base_url

_KEY = "sk-ant-owner-fixture-0123456789"
_CONNECTION = "mconn_" + "a" * 32

#: Base URL as typed -> ANTHROPIC_BASE_URL -> where the CLI posts.
_SHAPES = [
    ("https://api.example.com", "https://api.example.com"),
    ("https://api.example.com/v1", "https://api.example.com"),
    ("https://gateway.example/anthropic", "https://gateway.example/anthropic"),
    ("https://gateway.example/anthropic/v1", "https://gateway.example/anthropic"),
    # Only a final ``/v1`` segment is the SDK's; ``/v1beta`` or an inner one is the provider's.
    ("https://gateway.example/v1beta", "https://gateway.example/v1beta"),
    ("https://gateway.example/v1/anthropic", "https://gateway.example/v1/anthropic"),
]


def _prepare(base: str, **extra: object) -> tuple[list[str], dict[str, str]]:
    return ClaudeCodeRuntime().prepare_live(
        workdir="/workspace",
        model="claude-sonnet-4-5",
        gateway_base_url=MessagesBaseUrl(base),
        credential=_KEY,
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(("base", "expected"), _SHAPES)
def test_the_cli_and_the_check_reach_the_same_messages_endpoint(base, expected):
    _argv, env = _prepare(base)
    assert env["ANTHROPIC_BASE_URL"] == expected == anthropic_base_url(base)
    # The SDK's own join, which the CLI was observed to make.
    assert messages_url(base) == f"{env['ANTHROPIC_BASE_URL']}/v1/messages?beta=true"
    assert models_url(base) == f"{expected}/v1/models"


def test_the_owner_key_and_model_reach_only_the_environment():
    argv, env = _prepare(
        "https://api.example.com/v1",
        mcp_config=RuntimeMCPConfig(
            server_ids=("notion",), base_url="http://mcp.local:9", credential="mcp-token"
        ),
    )
    assert env["ANTHROPIC_API_KEY"] == _KEY
    assert env["ANTHROPIC_MODEL"] == env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-4-5"
    assert all(_KEY not in arg for arg in argv)
    # The bearer spelling is never set, and no OpenAI name carries the key.
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert not any(name.startswith("OPENAI_") for name in env)
    # The MCP relay keeps its own scoped token, apart from the inference key.
    assert env[MCP_TOKEN_ENV] == "mcp-token"
    assert "mcp.local" in " ".join(argv) and "api.example.com" not in " ".join(argv)


def test_effort_stays_auto_on_a_connection():
    argv, _env = _prepare("https://api.example.com")
    assert "--effort" not in argv


def test_the_gateway_relay_is_unchanged_for_claude_code():
    _argv, env = ClaudeCodeRuntime().prepare_live(
        workdir="/workspace",
        model="glm-5.1",
        gateway_base_url="http://relay.local:8080/v1",
        credential="grant",
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://relay.local:8080"
    assert env["ANTHROPIC_API_KEY"] == "grant"


@pytest.mark.parametrize("runtime", [OpencodeRuntime(), KiloRuntime(), PiRuntime(), CodexRuntime()])
def test_an_openai_runtime_refuses_a_messages_connection(runtime):
    with pytest.raises(RuntimeConnectionUnsupportedError, match="Anthropic Messages"):
        runtime.prepare_live(
            workdir="/workspace",
            model="claude-sonnet-4-5",
            gateway_base_url=MessagesBaseUrl("https://api.example.com/v1"),
            credential=_KEY,
        )


def test_the_launch_context_types_the_url_by_its_protocol():
    assert type(connection_base_url("https://x.example/v1", "anthropic_messages")) is (
        MessagesBaseUrl
    )
    for protocol in ("chat_completions", "responses"):
        url = connection_base_url("https://x.example/v1", protocol)
        assert type(url) is ExactBaseUrl and url == "https://x.example/v1"


def test_a_messages_connection_launch_crosses_the_sandbox_manager_intact(monkeypatch):
    monkeypatch.setenv("AGENT_GATEWAY_URL", "http://10.0.0.1:8080")
    access = ModelConnectionAccess(
        connection_id=_CONNECTION,
        revision=2,
        base_url="https://gateway.example/anthropic/v1",
        protocol="anthropic_messages",
        api_key=_KEY,
    )
    body = SharedResidentLaunchConfig(
        runtime_id="claude-code",
        model="claude-sonnet-4-5",
        credential=_KEY,
        slot_id=1,
        **access.launch_fields(),
    )
    launch = resident_launch("athr_1", body)
    assert launch.inference_protocol == "anthropic_messages"
    assert launch.inference_base_url == "https://gateway.example/anthropic/v1"
    assert _KEY not in repr(body) and _KEY not in repr(launch)


def _launch(**changes: object) -> ResidentLaunch:
    fields: dict[str, object] = {
        "thread_id": "athr_1",
        "runtime_id": "claude-code",
        "runtime_version": None,
        "native_session_id": "native-1",
        "model": "claude-sonnet-4-5",
        "reasoning_effort": None,
        "gateway_base_url": "http://relay.local:8080",
        "credential": _KEY,
        "inference_base_url": "https://api.example.com/v1",
        "inference_protocol": "anthropic_messages",
        "inference_source": f"{_CONNECTION}@2",
    }
    fields.update(changes)
    return ResidentLaunch(**fields)  # type: ignore[arg-type]


def test_rotation_or_a_protocol_change_restarts_the_claude_member():
    launch = _launch()
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())
    assert transport.compatible_with(replace(launch, credential=None))
    assert not transport.compatible_with(replace(launch, inference_source=f"{_CONNECTION}@3"))
    assert not transport.compatible_with(replace(launch, inference_protocol="chat_completions"))
    gateway = replace(launch, inference_base_url="", inference_protocol="", inference_source="")
    assert not transport.compatible_with(gateway)
