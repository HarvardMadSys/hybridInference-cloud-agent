"""Regressions for the review findings on PR #190.

Each case here failed before its fix, and each is written against the *shown*
behaviour rather than the internal helper, because the findings were all of the
same shape: a check that existed, in the wrong place, or for one surface only.

- the invocation listing trusted the caller's ``job_id`` and decrypted the
  arguments it found;
- the per-connection relay authorized ``tools/call`` and forwarded everything
  else, so a resource the session had turned off was readable by asking the
  other endpoint;
- the relay dialled whatever URL the registry held, so the egress policy the
  transports enforce was not in the call path at all;
- an approval was matched on job, connection and arguments, and its versions
  were written down but never compared, so a v1 approval authorized a v2 target;
- ``set_mcp_credential`` wrote NULL over a refresh token it had just been given;
- every adapter pointed its CLI at the per-connection relay rather than the
  merged host, so a connection only the host can drive -- a legacy SSE server
  above all -- was reached by a route that forwards the protocol instead of
  speaking it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cloud_agent.mcp import oauth

pytestmark = []


class TestTheRedirectUri:
    """Register, authorize and redeem have to name the same callback."""

    def test_the_registered_uri_is_the_one_the_flow_uses(self) -> None:
        """A server compares them, and refuses a flow whose request differs.

        The bug was a registration carrying the bare callback path while the
        authorization carried the per-connection one -- a prefix of the right
        answer, which is the kind of difference that only shows at the browser.
        """
        from cloud_agent.api.mcp_connections import _callback_url

        base = "https://agent.example.com"
        registered = _callback_url(base, 8)
        authorization = oauth.redirect_uri(base, "8")
        assert registered == authorization
        assert registered.endswith("/oauth/callback/8")


class TestClientAuthentication:
    """The negotiated mechanism, not a preferred one."""

    def _request(self, method: str, secret: str = "s:cret") -> oauth.TokenRequest:
        return oauth.TokenRequest(
            url="https://auth.example.com/token",
            data={"grant_type": "authorization_code"},
            client=oauth.ClientIdentity(
                client_id="cid",
                registration="preregistered",
                client_secret=secret,
                auth_method=method,
            ),
        )

    def test_basic_sends_a_header_and_keeps_the_secret_out_of_the_body(self) -> None:
        """Both would be a third thing the server never agreed to."""
        request = self._request("client_secret_basic")
        assert "authorization" in request.headers()
        assert "client_secret" not in request.form()

    def test_the_basic_credentials_are_form_encoded_before_being_joined(self) -> None:
        """A colon in the secret is legal and would otherwise split it."""
        import base64

        request = self._request("client_secret_basic", secret="a:b")
        encoded = request.headers()["authorization"].removeprefix("Basic ")
        assert base64.b64decode(encoded).decode() == "cid:a%3Ab"

    def test_post_sends_the_secret_in_the_body_and_no_header(self) -> None:
        """The mechanism this platform picked before, still available."""
        request = self._request("client_secret_post")
        assert request.form()["client_secret"] == "s:cret"
        assert "authorization" not in request.headers()

    def test_a_public_client_sends_neither(self) -> None:
        """There is no secret to send, and an empty one is not a credential."""
        request = oauth.TokenRequest(
            url="https://auth.example.com/token",
            data={"grant_type": "authorization_code"},
            client=oauth.ClientIdentity(client_id="cid", registration="dcr"),
        )
        assert "client_secret" not in request.form()
        assert "authorization" not in request.headers()


class TestTheRuntimeRelayEntry:
    """Which endpoint the adapters point a CLI at.

    Every adapter published one entry per connection, at the relay's
    since-retired per-connection route. That route forwarded one connection's
    protocol untouched, so it could only serve a server that already speaks the
    shape the CLI speaks: a legacy SSE server answers the CLI's POST with 405,
    and one that replies to a call with ``InputRequiredResult`` is a protocol
    error the CLI has no way to continue. The merged endpoint is where the platform speaks
    the protocol itself, opening each session in whichever era its server turned
    out to speak, and it answers for every connection of the job.
    """

    _BASE = "http://agent-mcp:8000"

    def _config(self, server_ids: tuple[str, ...]) -> Any:
        from cloud_agent_host.runtimes import RuntimeMCPConfig

        return RuntimeMCPConfig(server_ids=server_ids, base_url=self._BASE, credential="ajt-token")

    def test_claude_code_gets_one_entry_at_the_merged_endpoint(self) -> None:
        """The exact URL matters: an alias in it would put the protocol back in the CLI."""
        from cloud_agent_host.runtimes import MERGED_MCP_SERVER_NAME, ClaudeCodeRuntime

        argv, _env = ClaudeCodeRuntime().prepare_live(
            workdir="/w",
            model="glm-5.1",
            model_base_url="http://backend:8080",
            credential="agr-credential",
            mcp_config=self._config(("github", "docs")),
        )
        servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
        assert servers == {
            MERGED_MCP_SERVER_NAME: {
                "type": "http",
                "url": f"{self._BASE}/v1/agent/mcp",
                "timeout": 1560000,
                "headers": {"Authorization": "Bearer ${AGENT_MCP_TOKEN}"},
            }
        }

    @pytest.mark.parametrize("runtime_name", ["OpencodeRuntime", "PiRuntime"])
    def test_the_wrapper_runtimes_get_the_same_single_entry(self, runtime_name: str) -> None:
        """Two connections, one URL: the merged host already namespaces by alias."""
        from cloud_agent_host import runtimes

        runtime = getattr(runtimes, runtime_name)()
        _argv, env = runtime.prepare_live(
            workdir="/w",
            model="glm-5.1",
            model_base_url="http://backend:8080",
            credential="agr-credential",
            mcp_config=self._config(("github", "docs")),
        )
        assert json.loads(env[runtimes.MCP_SERVERS_ENV]) == {
            runtimes.MERGED_MCP_SERVER_NAME: f"{self._BASE}/v1/agent/mcp"
        }

    def test_a_run_with_no_connections_is_not_told_about_the_relay(self) -> None:
        """An endpoint a run must not use is an endpoint it should not be given."""
        from cloud_agent_host.runtimes import ClaudeCodeRuntime

        argv, _env = ClaudeCodeRuntime().prepare_live(
            workdir="/w",
            model="glm-5.1",
            model_base_url="http://backend:8080",
            credential="agr-credential",
            mcp_config=self._config(()),
        )
        assert json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"] == {}


class TestTheMergedHostDrivesWhatAPipeCannot:
    """Why the endpoint matters, not just that it changed.

    The per-connection route posted the CLI's own body at the connection's URL.
    A legacy SSE server has no such URL: its SSE address answers 405 to that
    POST, and the address that would have accepted it is the message endpoint
    the stream names after connecting. Only a client that reads the era first
    can serve one, which is what the host does and what the route did not.
    """

    def _connection(self, transport: Any) -> Any:
        from cloud_agent.mcp.models import (
            ConnectionConfig,
            ExecutionLocation,
            McpConnection,
            Transport,
        )

        declared = Transport.HTTP_SSE if transport == "http-sse" else Transport.STREAMABLE_HTTP
        return McpConnection(
            connection_id="7",
            user_id="u1",
            label="Legacy",
            config=ConnectionConfig(
                transport=declared,
                location=ExecutionLocation.REMOTE,
                url="http://127.0.0.1:9/sse"
                if declared is Transport.HTTP_SSE
                else "http://127.0.0.1:9/mcp",
            ),
        )

    @pytest.mark.parametrize(
        ("transport", "expected"),
        [("http-sse", "legacy"), ("streamable-http", "streamable")],
    )
    @pytest.mark.asyncio
    async def test_the_transport_the_connection_declares_is_the_one_opened(
        self, transport: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_agent.mcp import host as host_module

        calls: list[str] = []

        async def legacy(config: Any, **kwargs: Any) -> Any:
            calls.append("legacy")
            return ("transport", "handshake", None)

        async def streamable(config: Any, **kwargs: Any) -> Any:
            calls.append("streamable")
            return ("transport", "handshake")

        monkeypatch.setattr(host_module, "connect_legacy_sse", legacy)
        monkeypatch.setattr(host_module, "connect_streamable", streamable)
        await host_module.McpHost()._connect(self._connection(transport), secret="token")
        assert calls == [expected]
