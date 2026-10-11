"""The gateway client — every question this service used to answer by import.

Tested at this level because three consumers depend on the same failure
behaviour: the visible-model catalog, the session's user status, and the MCP
registry. If the client ever failed *open* — treating an unreachable gateway as
permission — each of them would inherit that quietly.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from cloud_agent.api.deps import resolve_agent_access
from cloud_agent.gateway import ENV_BASE_URL, ENV_DISPATCH_TOKEN, GatewayClient, GatewayError

BASE = "https://gateway.example"
TOKEN = "dispatch-token"


@pytest.mark.parametrize("body", [{}, {"error": {"type": "browser_check"}}, "Forbidden"])
@pytest.mark.parametrize("reject_unavailable_subject", [False, True])
async def test_proxy_403_is_unavailable_rather_than_a_policy_denial(
    body, reject_unavailable_subject
):
    with pytest.raises(HTTPException) as raised:
        await resolve_agent_access(
            _client(_serves(body, 403)),
            "user_1",
            reject_unavailable_subject=reject_unavailable_subject,
        )
    assert raised.value.status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"type": "subject_unavailable"}},
        {"detail": {"error": {"type": "subject_unavailable"}}},
    ],
)
async def test_only_an_explicit_subject_refusal_is_an_access_denial(body):
    client = _client(_serves(body, 403))
    with pytest.raises(GatewayError) as raised:
        await client.agent_access("user_1")
    assert raised.value.error_type == "subject_unavailable"
    decision = await resolve_agent_access(client, "user_1")
    assert decision.allowed is False
    assert decision.permissions == []
    with pytest.raises(HTTPException) as refused:
        await resolve_agent_access(client, "user_1", reject_unavailable_subject=True)
    assert refused.value.status_code == 403


async def test_an_html_proxy_403_is_unavailable():
    client = _client(lambda _: httpx.Response(403, text="<html>Forbidden</html>"))
    with pytest.raises(HTTPException) as raised:
        await resolve_agent_access(client, "user_1")
    assert raised.value.status_code == 503


@pytest.mark.parametrize("status", [401, 404])
async def test_incompatible_or_misconfigured_gateway_is_reported_as_configuration(status):
    with pytest.raises(HTTPException) as raised:
        await resolve_agent_access(_client(_serves({}, status)), "user_1")
    assert raised.value.status_code == 503
    assert raised.value.detail["error"]["type"] == "agent_configuration_error"


async def test_missing_gateway_configuration_is_not_a_temporary_failure(monkeypatch):
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    monkeypatch.delenv(ENV_DISPATCH_TOKEN, raising=False)
    with pytest.raises(HTTPException) as raised:
        await resolve_agent_access(GatewayClient(), "user_1")
    assert raised.value.detail["error"]["type"] == "agent_configuration_error"


def _client(handler) -> GatewayClient:
    return GatewayClient(
        BASE, TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _serves(payload: Any, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_configured_requires_both_halves() -> None:
    assert GatewayClient(BASE, TOKEN).configured is True
    assert GatewayClient(BASE, "").configured is False
    assert GatewayClient("", TOKEN).configured is False


def test_configuration_falls_back_to_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, BASE)
    monkeypatch.setenv(ENV_DISPATCH_TOKEN, TOKEN)
    assert GatewayClient().configured is True


async def test_a_trailing_slash_does_not_produce_a_double_slash() -> None:
    """A `//internal/...` path is a 404 that reads like a missing endpoint."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"user_id": "user_1", "allowed": True, "permissions": ["agent.use"]}
        )

    client = GatewayClient(
        f"{BASE}/", TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await client.agent_access("user_1")
    assert seen["path"] == "/internal/users/user_1/agent-access"


# ---------------------------------------------------------------------------
# Every method fails closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500, 503])
async def test_a_non_200_raises(status_code: int) -> None:
    with pytest.raises(GatewayError):
        await _client(_serves({}, status_code)).agent_access("user_1")


async def test_unparsable_json_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    with pytest.raises(GatewayError):
        await _client(handler).agent_access("user_1")


async def test_a_non_object_body_raises() -> None:
    """A list where an object belongs is a contract change, not a value."""
    with pytest.raises(GatewayError):
        await _client(_serves([1, 2, 3])).agent_access("user_1")


async def test_a_transport_failure_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(GatewayError):
        await _client(handler).model_catalog("user_1")


async def test_an_unconfigured_client_raises_before_any_request() -> None:
    with pytest.raises(GatewayError, match=ENV_BASE_URL):
        await GatewayClient("", "").agent_access("user_1")


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


async def test_access_returns_the_current_permission_decision() -> None:
    record = {"user_id": "user_1", "allowed": True, "permissions": ["agent.use"]}
    assert (await _client(_serves(record)).agent_access("user_1")).model_dump() == record


async def test_model_configuration_reads_one_current_owner_scoped_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        model = f"{request.url.params['user_id']}-model-{len(requests)}"
        return httpx.Response(
            200,
            json={
                "models": [model],
                "reasoning_efforts": {
                    model: ["low", "high"],
                    "unavailable-model": ["max"],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = GatewayClient(BASE, TOKEN, client=http)
        for index, owner in enumerate(("user_1", "user_1", "user_2"), start=1):
            model = f"{owner}-model-{index}"
            assert await client.model_catalog_with_reasoning_efforts(owner) == (
                [model],
                {model: ["low", "high"]},
            )
            assert len(requests) == index
    assert [request.url.params["user_id"] for request in requests] == [
        "user_1",
        "user_1",
        "user_2",
    ]
    assert all(request.url.path == "/internal/model-catalog" for request in requests)
    assert all(request.headers["Authorization"] == f"Bearer {TOKEN}" for request in requests)


@pytest.mark.parametrize(
    "efforts",
    [None, "low", {"visible": [], "other": ["high"]}, {"visible": "high"}],
)
async def test_model_configuration_ignores_missing_or_unusable_effort_domains(efforts) -> None:
    payload = {"models": ["visible"], "reasoning_efforts": efforts}
    assert await _client(_serves(payload)).model_catalog_with_reasoning_efforts("user_1") == (
        ["visible"],
        {},
    )


async def test_model_configuration_accepts_a_legacy_catalog_without_efforts() -> None:
    assert await _client(_serves({"models": ["visible"]})).model_catalog_with_reasoning_efforts(
        "user_1"
    ) == (["visible"], {})


@pytest.mark.parametrize("models", [None, "visible", {"visible": True}])
async def test_model_configuration_refuses_a_malformed_model_list(models) -> None:
    with pytest.raises(GatewayError, match="no model list"):
        await _client(_serves({"models": models})).model_catalog_with_reasoning_efforts("user_1")


# Two `mcp_registry` tests stood here — one dropping malformed entries, one
# refusing a response with no list. Both went with the method: the gateway no
# longer serves `/internal/mcp-registry`.
#
# What they guarded is covered more strictly where MCP now lives, and by
# different code rather than a moved copy: there is no deployment registry and
# no in-code provider catalogue any more, so a malformed *entry* is refused by
# `tests/unit/test_mcp_connection_config.py` (address syntax, header
# injection, an unpinned package) and by
# `tests/unit/test_mcp_connections_api.py` (an unknown transport, a loopback
# target, a stdio connection with nothing to run). Checked case by case rather
# than assumed, because "the other file covers it" is how coverage disappears
# during a rewrite.
