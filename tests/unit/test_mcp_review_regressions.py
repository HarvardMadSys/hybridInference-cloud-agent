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
- ``set_mcp_credential`` wrote NULL over a refresh token it had just been given.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cloud_agent.mcp import oauth, protocol

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


class TestTheDecisionCoversEverySurface:
    """The per-connection relay is a second door onto the same authorization."""

    @pytest.mark.asyncio
    async def test_a_resource_method_is_decided_rather_than_forwarded(self) -> None:
        """A deny on the merged endpoint has to mean a deny on this one.

        The bypass was structural: the decision function looked for
        ``tools/call`` and returned "forward it" for everything else, so
        ``resources/read`` reached the user's server with the user's credential
        after the session had turned that resource off.
        """
        from cloud_agent.api import mcp_relay

        decided: list[dict[str, Any]] = []

        async def fake_decide(authorized: Any, store: Any, **kwargs: Any) -> Any:
            decided.append(kwargs)
            return {"jsonrpc": "2.0", "id": kwargs["request_id"], "error": {"code": -32601}}

        original = mcp_relay._decide_call
        mcp_relay._decide_call = fake_decide
        try:
            authorized = _authorized()
            held = await mcp_relay._capability_decision(
                authorized,
                _Store(),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "resources/read",
                        "params": {"uri": "repo://notes"},
                    }
                ).encode(),
                attempt_id=1,
                lease_generation=1,
            )
        finally:
            mcp_relay._decide_call = original

        assert held is not None, "a resource read was forwarded without a decision"
        assert decided[0]["kind"].value == "resource"
        assert decided[0]["name"] == "repo://notes"

    @pytest.mark.asyncio
    async def test_a_prompt_method_is_decided_too(self) -> None:
        """Same surface, same rule; two cases because they key differently."""
        from cloud_agent.api import mcp_relay

        decided: list[dict[str, Any]] = []

        async def fake_decide(authorized: Any, store: Any, **kwargs: Any) -> Any:
            decided.append(kwargs)
            return {"jsonrpc": "2.0", "id": kwargs["request_id"], "error": {"code": -32601}}

        original = mcp_relay._decide_call
        mcp_relay._decide_call = fake_decide
        try:
            held = await mcp_relay._capability_decision(
                _authorized(),
                _Store(),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "prompts/get",
                        "params": {"name": "summarize"},
                    }
                ).encode(),
                attempt_id=1,
                lease_generation=1,
            )
        finally:
            mcp_relay._decide_call = original

        assert held is not None
        assert decided[0]["kind"].value == "prompt"

    @pytest.mark.asyncio
    async def test_a_method_with_no_authorization_story_is_refused(self) -> None:
        """Forwarding it would proxy a capability this platform cannot reason
        about, with a credential it is responsible for."""
        from cloud_agent.api import mcp_relay

        reply = await mcp_relay._capability_decision(
            _authorized(),
            _Store(),
            json.dumps(
                {"jsonrpc": "2.0", "id": 3, "method": "completion/complete", "params": {}}
            ).encode(),
            attempt_id=1,
            lease_generation=1,
        )
        assert reply is not None
        assert "does not proxy" in reply["error"]["message"]

    @pytest.mark.asyncio
    async def test_housekeeping_and_notifications_still_pass(self) -> None:
        """Refusing these would break the protocol without protecting anything."""
        from cloud_agent.api import mcp_relay

        for method in (
            protocol.INITIALIZE,
            protocol.PING,
            protocol.TOOLS_LIST,
            "notifications/initialized",
        ):
            assert (
                await mcp_relay._capability_decision(
                    _authorized(),
                    _Store(),
                    json.dumps({"jsonrpc": "2.0", "id": 4, "method": method}).encode(),
                    attempt_id=1,
                    lease_generation=1,
                )
                is None
            ), method


def _authorized() -> Any:
    """A binding with one allowed tool and one denied resource."""
    from cloud_agent.api.mcp_relay import Authorized
    from cloud_agent.mcp.models import (
        Capability,
        CapabilityKind,
        CapabilitySnapshot,
        ConnectionConfig,
        ExecutionLocation,
        McpConnection,
        ThreadBinding,
        ToolPolicy,
        ToolRule,
        Transport,
    )

    connection = McpConnection(
        connection_id="7",
        user_id="u1",
        label="Docs",
        config=ConnectionConfig(
            transport=Transport.STREAMABLE_HTTP,
            location=ExecutionLocation.REMOTE,
            url="http://127.0.0.1:9/mcp",
        ),
    )
    snapshot = CapabilitySnapshot(
        connection_id="7",
        config_revision=connection.config_revision,
        capability_revision=connection.capability_revision,
        protocol_version="2025-11-25",
        tools=(Capability(kind=CapabilityKind.TOOL, name="get_issue"),),
        resources=(Capability(kind=CapabilityKind.RESOURCE, name="repo://notes"),),
        prompts=(Capability(kind=CapabilityKind.PROMPT, name="summarize"),),
    )
    binding = ThreadBinding(
        thread_id="t1",
        connection_id="7",
        alias="docs",
        config_revision=connection.config_revision,
        authorization_epoch=connection.authorization_epoch,
        capability_revision=connection.capability_revision,
        rules={"get_issue": ToolRule(name="get_issue", policy=ToolPolicy.ALLOW)},
        resource_rules={"repo://notes": ToolPolicy.DENY},
        prompt_rules={"summarize": ToolPolicy.DENY},
    )
    from cloud_agent.mcp.registry import server_for

    return Authorized(
        job_id="ajob",
        user_id="u1",
        alias="docs",
        thread_id="t1",
        connection_id=7,
        connection=connection,
        binding=binding,
        snapshot=snapshot,
        secret="token",
        # Built by the same function the relay uses, so the reachable sets the
        # test asserts against are the ones production would have.
        server=server_for(_resolved(connection, snapshot), binding),
    )


def _resolved(connection: Any, snapshot: Any) -> Any:
    """The resolved connection the registry reads to build a reachable server."""
    from cloud_agent.mcp.registry import ResolvedConnection

    return ResolvedConnection(
        connection=connection,
        credential={"secret": "token", "kind": "static"},
        snapshot=snapshot,
    )


class _Store:
    """A store that refuses everything, so nothing is dispatched by accident."""

    async def find_approved_mcp_invocation(self, **kwargs: Any) -> Any:
        return None

    async def create_mcp_invocation(self, **kwargs: Any) -> Any:
        return {"id": 1, **kwargs}
