"""A direct plan's CLI behind its member's request meter: the URL it is told, and its launch."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.support.owners import launch_connection

from cloud_agent_host.launch.inference import InferenceFacet
from cloud_agent_host.member_setup.compat_helper import LAUNCHER
from cloud_agent_host.model_passthrough import PassthroughBinding
from cloud_agent_host.resident_transport import ResidentLaunch
from cloud_agent_host.runtimes import (
    ClaudeCodeRuntime,
    CodexRuntime,
    ExactBaseUrl,
    KiloRuntime,
    MessagesBaseUrl,
    OpencodeRuntime,
    PiRuntime,
    connection_base_url,
    meter_launch,
    meters_direct_plan,
)
from cloud_agent_host.runtimes.base import metered_cli_url
from cloud_agent_shared.protocol_compat import ORIGIN_PLACEHOLDER

_KEY = "sk-owner-key-0123456789"
_URL = "https://api.provider.example/v1"


def test_a_metered_plan_keeps_its_protocol_type():
    chat = connection_base_url(_URL, "chat_completions", metered=True)
    messages = connection_base_url(_URL, "anthropic_messages", metered=True)
    assert isinstance(chat, ExactBaseUrl) and chat.metered
    assert isinstance(messages, MessagesBaseUrl) and messages.metered
    assert not connection_base_url(_URL, "chat_completions").metered
    # A translated plan's helper measures it already; it is never "metered" too.
    assert not connection_base_url(_URL, "chat_completions", "responses", metered=True).metered


def test_the_cli_url_keeps_the_providers_authority_and_path():
    assert metered_cli_url(_URL) == f"{ORIGIN_PLACEHOLDER}/api.provider.example/v1"
    assert metered_cli_url("https://Host.example:8443/api/v1") == (
        f"{ORIGIN_PLACEHOLDER}/host.example:8443/api/v1"
    )
    # What the launcher writes over the placeholder, and what the meter makes of it.
    binding = PassthroughBinding(host="host.example", port=8443)
    url = metered_cli_url("https://Host.example:8443/api/v1").replace(ORIGIN_PLACEHOLDER, "")
    assert binding.upstream_path(url + "/chat/completions") == "/api/v1/chat/completions"


def test_metered_claude_keeps_its_key_and_is_pointed_at_its_meter():
    argv, env = ClaudeCodeRuntime().prepare_live(
        workdir="/workspace",
        model="",
        model_base_url=connection_base_url(
            "https://api.provider.example/anthropic/v1", "anthropic_messages", metered=True
        ),
        credential=_KEY,
    )
    assert argv[0] == "claude"
    assert env["ANTHROPIC_BASE_URL"] == f"{ORIGIN_PLACEHOLDER}/api.provider.example/anthropic"
    assert env["ANTHROPIC_API_KEY"] == _KEY
    assert not any(name.startswith("AGENT_COMPAT_") for name in env)


def test_metered_codex_keeps_its_key_and_is_pointed_at_its_meter():
    argv, env = CodexRuntime().prepare_live(
        workdir="/workspace",
        model="gpt-x",
        model_base_url=connection_base_url(_URL, "responses", metered=True),
        credential=_KEY,
    )
    marked = f"{ORIGIN_PLACEHOLDER}/api.provider.example/v1"
    assert f'base_url="{marked}"' in " ".join(argv)
    assert env["OPENAI_BASE_URL"] == marked and env["CODEX_API_KEY"] == _KEY
    assert "web_search" not in " ".join(argv)


@pytest.mark.parametrize("runtime", [OpencodeRuntime(), KiloRuntime(), PiRuntime()])
def test_metered_chat_runtimes_are_pointed_at_their_meter(runtime):
    _, env = runtime.prepare_live(
        workdir="/w",
        model="m",
        model_base_url=connection_base_url(_URL, "chat_completions", metered=True),
        credential=_KEY,
    )
    # The provider's name stays in the URL a CLI may recognize it by.
    assert env["OPENAI_BASE_URL"] == f"{ORIGIN_PLACEHOLDER}/api.provider.example/v1"
    assert env["OPENAI_API_KEY"] == _KEY


def test_the_launch_hands_the_meter_an_origin_and_no_key():
    argv, env = meter_launch(["claude", "-p"], {"ANTHROPIC_API_KEY": _KEY}, _URL)
    assert argv == [*LAUNCHER, "claude", "-p"]
    assert env == {
        "ANTHROPIC_API_KEY": _KEY,
        "AGENT_COMPAT_CLIENT": "passthrough",
        "AGENT_COMPAT_UPSTREAM_URL": "https://api.provider.example",
    }


def test_a_cli_on_its_vendors_own_host_is_not_metered():
    # Claude Code decides features from the host it is given; it keeps seeing its own.
    assert not meters_direct_plan("claude-code", "https://api.anthropic.com")
    assert not meters_direct_plan("claude-code", "https://API.anthropic.com/v1")
    assert meters_direct_plan("claude-code", "https://api.deepseek.com/anthropic")
    assert meters_direct_plan("codex", "https://api.openai.com/v1")
    assert meters_direct_plan("pi", "https://api.anthropic.com/v1")


def _metered(runtime_id: str, base_url: str, client_protocol: str = "") -> bool:
    launch = replace(
        ResidentLaunch(
            thread_id="athr_owner",
            runtime_id=runtime_id,
            runtime_version=None,
            native_session_id=None,
            model="model",
            reasoning_effort=None,
            credential="grant",
            **launch_connection(runtime_id),
        ),
        inference_base_url=base_url,
        inference_client_protocol=client_protocol,
        # A translated launch carries its member's loopback token.
        inference_local_token="t" * 64 if client_protocol else "",
    )
    return launch.facet(InferenceFacet).metered


def test_only_a_direct_resident_launch_is_metered():
    assert _metered("codex", _URL)
    # A translated plan's helper measures it already.
    assert not _metered("codex", _URL, "responses")
    assert not _metered("claude-code", "https://api.anthropic.com")
