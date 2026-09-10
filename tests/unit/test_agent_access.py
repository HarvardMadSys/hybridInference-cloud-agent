"""Strict admission responses cannot promote callers through schema drift."""

from __future__ import annotations

import httpx
import pytest

from cloud_agent.gateway import GatewayClient, GatewayError


async def test_access_uses_dispatch_auth_and_encodes_the_subject():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "user_id": "user/one",
                "allowed": True,
                "permissions": ["agent.use", "agent.admin"],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        decision = await GatewayClient(
            "https://gateway.example", "dispatch-test", client=http
        ).agent_access("user/one")
    assert seen[0].url.raw_path == b"/internal/users/user%2Fone/agent-access"
    assert seen[0].headers["authorization"] == "Bearer dispatch-test"
    assert decision.permits("agent.use")
    assert decision.permits("agent.admin")
    assert not decision.permits("admin")
    assert not decision.permits("unknown.permission")


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "owner", "allowed": True},
        {"user_id": "owner", "allowed": "true", "permissions": ["agent.use"]},
        {"user_id": "owner", "allowed": True, "permissions": "agent.use"},
        {"user_id": "owner", "allowed": True, "permissions": ["agent.use", "agent.use"]},
        {"user_id": "owner", "allowed": True, "permissions": ["agent.use", "superadmin"]},
        {"user_id": "owner", "allowed": True, "permissions": ["agent.admin"]},
        {"user_id": "owner", "allowed": True, "permissions": []},
        {"user_id": "owner", "allowed": False, "permissions": ["agent.use"]},
        {"user_id": "someone-else", "allowed": True, "permissions": ["agent.use"]},
        {"user_id": "owner", "allowed": True, "permissions": ["agent.use"], "role": "admin"},
        {"user_id": 1, "allowed": True, "permissions": ["agent.use"]},
        {"role": "admin", "active": True},
    ],
)
async def test_malformed_or_role_only_decisions_never_grant_access(payload):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        client = GatewayClient("https://gateway.example", "dispatch-test", client=http)
        with pytest.raises(GatewayError):
            await client.agent_access("owner")


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
async def test_missing_or_failed_access_endpoint_never_tries_user_status(status):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(status, json={"role": "admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayClient("https://gateway.example", "dispatch-test", client=http)
        with pytest.raises(GatewayError) as caught:
            await client.agent_access("owner")
    assert caught.value.status_code == status
    assert paths == ["/internal/users/owner/agent-access"]


async def test_explicit_deny_is_a_valid_decision():
    payload = {"user_id": "owner", "allowed": False, "permissions": []}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        decision = await GatewayClient(
            "https://gateway.example", "dispatch-test", client=http
        ).agent_access("owner")
    assert not decision.permits("agent.use")
    assert not decision.permits("agent.admin")
