"""A member on a provided connection: launched at the host's model relay, holding a placeholder."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from tests.unit.test_agent_runner_capabilities import _configure_host, _stamp

from cloud_agent_host.claimed_job import (
    ClaimedJob,
    claim_secret_values,
    model_connection_launch,
    require_residency_connection,
)
from cloud_agent_host.container.sandbox import ContainerBackend, SandboxError
from cloud_agent_host.model_passthrough import PassthroughBinding, _PassthroughServer
from cloud_agent_host.protocol_translator import Binding, LaunchError
from cloud_agent_host.resident_launch_config import SharedResidentLaunchConfig, resident_launch
from cloud_agent_host.resident_transport import ResidentLaunch, ResidentTransport
from cloud_agent_host.runtimes import meter_launch, meters_direct_plan
from cloud_agent_host.runtimes.base import (
    ResponsesViaChatUrl,
    compat_launch,
    metered_cli_url,
)
from cloud_agent_host.service import host_capabilities
from cloud_agent_shared.egress import RelayEndpoint, SandboxNetworkError, network_from_env
from cloud_agent_shared.model_connections import ModelConnectionAccess
from cloud_agent_shared.protocol_compat import (
    ENV_CLIENT,
    ENV_UPSTREAM_KEY,
    ENV_UPSTREAM_RELAY,
    ENV_UPSTREAM_URL,
    ORIGIN_PLACEHOLDER,
)

_PROVIDED = "oconn_" + "d" * 32
_OWNER = "mconn_" + "c" * 32
_PLACEHOLDER = "amd.eyJ0IjoiYXRocl8xIn0.c2lnbmF0dXJl"
_RELAY = "http://agent-models:8000"
_ACCESS = ModelConnectionAccess(
    connection_id=_PROVIDED,
    revision=3,
    base_url="https://provider.example/api/v1",
    protocol="chat_completions",
    relayed=True,
)


def _body(**changes: Any) -> SharedResidentLaunchConfig:
    fields = {
        "runtime_id": "opencode",
        "model": "model-1",
        "credential": _PLACEHOLDER,
        "slot_id": 1,
        **_ACCESS.launch_fields(),
        **changes,
    }
    return SharedResidentLaunchConfig(**fields)


# -- the claim ---------------------------------------------------------------------


def test_a_relayed_claim_puts_no_key_on_the_host() -> None:
    job = ClaimedJob.from_response(
        {
            "job_id": "ajob_1",
            "thread_id": "athr_1",
            "attempt_id": 1,
            "attempt_no": 1,
            "repo": "owner/repo",
            "task_prompt": "do it",
            "runtime": "opencode",
            "model": "model-1",
            "worker_token": "w",
            "model_connection": _ACCESS.model_dump(),
        }
    )
    assert job.model_connection == _ACCESS
    # Nothing to mask from the claim: the placeholder arrives with the residency.
    assert claim_secret_values(job) == {}
    assert model_connection_launch(job)["inference_relayed"] == "1"
    # The residency must still name the revision and plan the claim named.
    require_residency_connection(
        job,
        {
            "model_connection_revision": 3,
            "model_connection_plan": "chat_completions>chat_completions",
        },
    )
    with pytest.raises(ValueError, match="changed"):
        require_residency_connection(
            job,
            {
                "model_connection_revision": 4,
                "model_connection_plan": "chat_completions>chat_completions",
            },
        )


# -- the launch --------------------------------------------------------------------


def test_the_sandbox_manager_launches_it_at_its_own_model_relay(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SANDBOX_MODEL_RELAY_URL", _RELAY + "/")
    launch = resident_launch("athr_1", _body())
    # The connection's own path, on this host's relay: never the provider's host.
    assert launch.inference_base_url == "http://agent-models:8000/api/v1"
    assert launch.inference_source == f"{_PROVIDED}@3"
    assert launch.inference_relayed == "1"
    assert launch.credential == _PLACEHOLDER
    assert _PLACEHOLDER not in repr(launch)


@pytest.mark.parametrize(
    "relay", ["", "https://agent-models:8000", "http://agent-models:8000/v1", "agent-models"]
)
def test_a_host_without_a_plain_relay_origin_refuses_the_launch(monkeypatch, relay) -> None:
    monkeypatch.setenv("AGENT_SANDBOX_MODEL_RELAY_URL", relay)
    with pytest.raises(SandboxError, match="AGENT_SANDBOX_MODEL_RELAY_URL"):
        resident_launch("athr_1", _body())


def test_only_a_provided_connection_is_relayed(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SANDBOX_MODEL_RELAY_URL", _RELAY)
    # An owner's connection marked relayed, or a provided one not marked.
    for fields in (
        {"inference_source": f"{_OWNER}@3"},
        {"inference_relayed": ""},
    ):
        with pytest.raises(SandboxError, match="relayed"):
            resident_launch("athr_1", _body(**fields))
    # The claim's Base URL is still a provider's https URL, never a relay's own.
    for bad in (
        {"inference_base_url": "http://agent-models:8000/v1"},
        {"inference_relayed": "yes"},
    ):
        with pytest.raises(ValidationError):
            _body(**bad)


def test_an_owner_connection_is_untouched_by_the_relay_setting(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SANDBOX_MODEL_RELAY_URL", _RELAY)
    owner = ModelConnectionAccess(
        connection_id=_OWNER,
        revision=1,
        base_url="https://provider.example/api/v1",
        protocol="chat_completions",
        api_key="sk-owner-key-0123456789",
    )
    launch = resident_launch(
        "athr_1",
        _body(credential="sk-owner-key-0123456789", inference_relayed="", **owner.launch_fields()),
    )
    assert launch.inference_base_url == "https://provider.example/api/v1"
    assert launch.inference_relayed == ""


def _launch(**changes: Any) -> ResidentLaunch:
    fields: dict[str, Any] = {
        "thread_id": "athr_1",
        "runtime_id": "opencode",
        "runtime_version": None,
        "native_session_id": "native-1",
        "model": "model-1",
        "reasoning_effort": None,
        "credential": _PLACEHOLDER,
        "inference_base_url": "http://agent-models:8000/api/v1",
        "inference_protocol": "chat_completions",
        "inference_source": f"{_PROVIDED}@3",
        "inference_relayed": "1",
        **changes,
    }
    return ResidentLaunch(**fields)


def test_a_process_is_never_borrowed_across_the_two_kinds_of_source() -> None:
    launch = _launch()
    transport = ResidentTransport(launch=launch, session=SimpleNamespace())
    assert transport.compatible_with(replace(launch, credential=None))
    assert transport.incompatible_fields(
        replace(launch, credential=None, inference_relayed="")
    ) == ("inference_relayed",)
    assert not transport.compatible_with(
        replace(launch, credential=None, inference_source=f"{_PROVIDED}@4")
    )


# -- the member's loopback service -------------------------------------------------


def test_a_relayed_cli_runs_behind_its_meter_like_any_other() -> None:
    url = "http://agent-models:8000/api/v1"
    # The relay is nobody's first-party host, so Claude Code is metered on it too.
    assert meters_direct_plan("claude-code", url)
    argv, env = meter_launch(["opencode", "run"], {"A": "1"}, url)
    assert argv[-2:] == ["opencode", "run"]
    assert env[ENV_UPSTREAM_URL] == "http://agent-models:8000"
    assert env[ENV_UPSTREAM_RELAY] == "1"
    assert metered_cli_url(url) == f"{ORIGIN_PLACEHOLDER}/agent-models:8000/api/v1"

    binding = PassthroughBinding.from_launch(dict(env) | {ENV_CLIENT: env[ENV_CLIENT]})
    assert (binding.host, binding.port, binding.tls) == ("agent-models", 8000, False)
    assert binding.origin == "http://agent-models:8000"
    # The URL the CLI is told and the path the meter answers are one spelling.
    cli_path = metered_cli_url(url).removeprefix(ORIGIN_PLACEHOLDER)
    assert binding.upstream_path(cli_path + "/chat/completions") == "/api/v1/chat/completions"


def test_a_relay_named_without_a_port_is_spelled_one_way_on_both_sides() -> None:
    url = "http://agent-models/v1"
    _argv, env = meter_launch(["cli"], {}, url)
    binding = PassthroughBinding.from_launch(dict(env))
    assert (binding.port, binding.authority) == (80, "agent-models:80")
    cli_path = metered_cli_url(url).removeprefix(ORIGIN_PLACEHOLDER)
    assert binding.upstream_path(cli_path + "/models") == "/v1/models"


def test_an_owner_connections_launch_is_what_it_was() -> None:
    _argv, env = meter_launch(["cli"], {}, "https://api.provider.example/v1")
    assert env[ENV_UPSTREAM_URL] == "https://api.provider.example"
    assert ENV_UPSTREAM_RELAY not in env
    binding = PassthroughBinding.from_launch(dict(env))
    assert (binding.port, binding.tls, binding.origin) == (
        443,
        True,
        "https://api.provider.example",
    )


@pytest.mark.parametrize(
    "launch",
    [
        # Plain http is the relay's alone, and the relay is never https.
        {ENV_UPSTREAM_URL: "http://agent-models:8000"},
        {ENV_UPSTREAM_URL: "http://provider.example", ENV_UPSTREAM_RELAY: "0"},
        {ENV_UPSTREAM_URL: "http://provider.example", ENV_UPSTREAM_RELAY: "true"},
        {ENV_UPSTREAM_URL: "https://provider.example", ENV_UPSTREAM_RELAY: "1"},
        {ENV_UPSTREAM_URL: "ftp://agent-models:8000", ENV_UPSTREAM_RELAY: "1"},
    ],
)
def test_the_launcher_accepts_http_only_for_the_relay(launch) -> None:
    with pytest.raises(LaunchError):
        PassthroughBinding.from_launch({ENV_CLIENT: "passthrough", **launch})
    translated = {
        ENV_CLIENT: "responses",
        ENV_UPSTREAM_KEY: _PLACEHOLDER,
        "AGENT_COMPAT_MODEL": "model-1",
        "AGENT_COMPAT_TOKEN": "a" * 64,
        **launch,
    }
    translated[ENV_UPSTREAM_URL] += "/v1"
    with pytest.raises(LaunchError):
        Binding.from_launch(translated, ["codex"])


def test_a_translated_member_reaches_the_relay_with_its_placeholder() -> None:
    url = ResponsesViaChatUrl("http://agent-models:8000/api/v1")
    argv, env = compat_launch(["codex"], {}, url, _PLACEHOLDER, model="model-1")
    assert env[ENV_UPSTREAM_RELAY] == "1" and env[ENV_UPSTREAM_KEY] == _PLACEHOLDER
    binding = Binding.from_launch({**env, "AGENT_COMPAT_TOKEN": "a" * 64}, argv)
    assert (binding.host, binding.port, binding.tls) == ("agent-models", 8000, False)
    assert binding.path == "/api/v1/chat/completions"
    # On an owner's connection nothing changes.
    _argv, direct = compat_launch(
        ["codex"], {}, ResponsesViaChatUrl("https://p.example/v1"), "sk-key", model="m"
    )
    assert ENV_UPSTREAM_RELAY not in direct
    assert Binding.from_launch({**direct, "AGENT_COMPAT_TOKEN": "a" * 64}, ["codex"]).tls


class _Relay:
    """The host's model relay, in the clear on loopback, recording what reaches it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                del args

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                relay.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": self.rfile.read(length),
                    }
                )
                answer = b'{"id":"answer"}'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_the_meter_forwards_to_the_relay_in_the_clear_with_the_cli_s_own_credential() -> None:
    relay = _Relay()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(4)
    binding = PassthroughBinding(host="127.0.0.1", port=relay.port, tls=False)
    server = _PassthroughServer(sock, binding)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", sock.getsockname()[1], timeout=10)
        body = json.dumps({"model": "model-1", "messages": []}).encode()
        connection.request(
            "POST",
            f"/{binding.authority}/api/v1/chat/completions",
            body=body,
            headers={"authorization": f"Bearer {_PLACEHOLDER}", "content-type": "application/json"},
        )
        response = connection.getresponse()
        assert (response.status, json.loads(response.read())) == (200, {"id": "answer"})
    finally:
        server.shutdown()
        server.server_close()
        relay.server.shutdown()
    [seen] = relay.requests
    assert seen["path"] == "/api/v1/chat/completions"
    # The meter holds no key: what the relay receives is the member's placeholder.
    assert seen["headers"]["authorization"] == f"Bearer {_PLACEHOLDER}"
    assert seen["body"] == body


# -- what the host says and builds -------------------------------------------------


def test_a_host_advertises_the_relay_only_when_its_sandboxes_reach_one(monkeypatch) -> None:
    _configure_host(monkeypatch)
    backend = ContainerBackend(image="img")
    monkeypatch.setattr(backend, "version_stamp", _stamp)
    monkeypatch.delenv("AGENT_SANDBOX_MODEL_RELAY_URL", raising=False)
    without = host_capabilities.claim_attestation(backend)["runner_capabilities"]
    assert "model-relay-v1" not in without

    monkeypatch.setenv("AGENT_SANDBOX_MODEL_RELAY_URL", _RELAY)
    with_relay = host_capabilities.claim_attestation(backend)["runner_capabilities"]
    assert with_relay == [*without, "model-relay-v1"]


def test_group_bridges_carry_the_model_relay_only_when_it_is_configured() -> None:
    env = {
        "AGENT_SANDBOX_NAMESPACE": "ns",
        "AGENT_EGRESS_CHAT_NETWORK_POOL": "10.240.0.0/16",
        "AGENT_EGRESS_DNS_SERVER": "192.0.2.53",
        "AGENT_RELAY_NETWORK": "relays",
        "AGENT_SANDBOX_MCP_URL": "http://agent-mcp:8000",
    }
    assert network_from_env(env).relays == (RelayEndpoint("agent-mcp", 8000),)
    with_relay = network_from_env({**env, "AGENT_SANDBOX_MODEL_RELAY_URL": _RELAY})
    assert with_relay.relays == (
        RelayEndpoint("agent-mcp", 8000),
        RelayEndpoint("agent-models", 8000),
    )
    # By name only, like every relay: a group bridge gives it a fresh address.
    with pytest.raises(SandboxNetworkError):
        network_from_env({**env, "AGENT_SANDBOX_MODEL_RELAY_URL": "http://10.0.0.5:8000"})
