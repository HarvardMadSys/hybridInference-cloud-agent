"""Who may call the MCP relay at all.

Every request is authorized against the attempt fence before the relay reads
anything else: the token must be an MCP-scoped job token whose attempt is still
live. These are the refusals at the one endpoint every runtime is given, and
the one door that stays open whatever a legacy rollout flag says.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.storage import OneFake

from cloud_agent.api import mcp_relay as agent_mcp_router
from cloud_agent.api.deps import get_storage
from cloud_agent.tokens import SCOPE_FULL, SCOPE_MCP, mint_worker_token

pytestmark = pytest.mark.asyncio

_FENCE = {"job_id": "ajob_abc", "attempt_id": 5, "lease_generation": 2}
_MCP = "/v1/agent/mcp"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    """Provide the signing secret job tokens are minted under."""
    monkeypatch.setenv("AGENT_CONTROL_TOKEN_SECRET", "mcp-relay-secret-value-32-bytes-x")


class FakeJobStore:
    """A fence that is live or not, for a job with no connections bound."""

    def __init__(self) -> None:
        self.live = True

    async def resolve_model_credential(
        self, *, job_id: str, attempt_id: int, lease_generation: int
    ) -> dict[str, Any] | None:
        if not self.live or (job_id, attempt_id, lease_generation) != tuple(_FENCE.values()):
            return None
        return {
            "user_id": "owner-1",
            "job_id": job_id,
            "thread_id": "athr_abc",
            "mcp_bindings": [],
            "model": "glm-5.1",
        }


@pytest.fixture()
def store() -> FakeJobStore:
    """A store resolving the test fence."""
    return FakeJobStore()


@pytest.fixture()
async def client(store):
    """A relay client over the fake store."""
    app = FastAPI()
    app.include_router(agent_mcp_router.router)
    app.dependency_overrides[get_storage] = lambda: OneFake(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
        yield api


def _auth(scope: str = SCOPE_MCP, **fence: int | str) -> dict[str, str]:
    """Authorization header for a job token."""
    return {"Authorization": f"Bearer {mint_worker_token(**{**_FENCE, **fence}, scope=scope)}"}


def _rpc(method: str) -> dict[str, Any]:
    """One JSON-RPC request body."""
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


async def test_a_dead_fence_stops_reaching_tools(store, client):
    """Cancelling a job revokes its tools exactly as it revokes inference."""
    store.live = False
    response = await client.post(_MCP, json=_rpc("initialize"), headers=_auth())
    assert response.status_code == 401


async def test_a_stale_lease_generation_stops_buying_tools(client):
    """The fence is read fresh on every call, so revocation is automatic.

    The moment the reaper supersedes the attempt or the owner cancels, the same
    token stops working -- there is no key to remember to revoke, which is the
    whole reason this service holds the fence rather than the gateway.
    """
    response = await client.post(_MCP, json=_rpc("tools/list"), headers=_auth(lease_generation=1))
    assert response.status_code == 401


async def test_an_ordinary_api_key_is_not_a_job_token(client):
    """This endpoint is for sandboxes; a user key is refused outright."""
    response = await client.post(
        _MCP, json=_rpc("initialize"), headers={"Authorization": "Bearer hyi-not-an-agent-token"}
    )
    assert response.status_code == 401


async def test_mcp_still_requires_its_own_token(client):
    """Always available still requires an authenticated, live attempt."""
    response = await client.post(_MCP, json=_rpc("tools/list"))
    assert response.status_code == 401


@pytest.mark.parametrize("scope", [SCOPE_FULL])
async def test_only_an_mcp_scoped_token_reaches_the_relay(client, scope):
    """**Positive, not "not full".**

    ``parse_worker_token`` reads a token minted before scopes existed as
    ``full``, which is the conservative reading when deciding whether a
    signature can be trusted and exactly the wrong one here -- a check written
    as "not full" would admit every such token to the tool surface.

    The runner's own ``full`` token is refused too. Reporting events and driving
    terminal transitions is not a reason to be able to call somebody's tools.
    """
    response = await client.post(_MCP, json=_rpc("tools/list"), headers=_auth(scope))

    assert response.status_code == 403
    # `{"detail": {"error": ...}}`, not `{"error": ...}`: this service installs
    # no handler that unwraps FastAPI's envelope, unlike the gateway.
    assert response.json()["detail"]["error"]["type"] == "insufficient_scope"


async def test_a_retired_model_scoped_token_never_reaches_the_relay(client):
    """The pre-grant ``model`` scope no longer parses at all (#822).

    Before the H4 cutover this relay's negative case was a minted
    ``SCOPE_MODEL`` token refused with 403. The scope is gone from the parser
    now, so the same correctly-signed legacy token is a 401: an escaped
    pre-cutover credential cannot even name itself here.
    """
    import hashlib
    import hmac as _hmac
    import json as _json

    from cloud_agent.tokens import _TOKEN_PREFIX, _b64encode, _signing_key

    payload = _json.dumps(
        {"j": "ajob_0001", "a": 1, "g": 1, "s": "model"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    # Signed correctly on purpose: the refusal below must be the parser's scope
    # check, not a signature failure.
    signature = _hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    legacy = f"{_TOKEN_PREFIX}.{_b64encode(payload)}.{_b64encode(signature)}"

    response = await client.post(
        _MCP, json=_rpc("tools/list"), headers={"Authorization": f"Bearer {legacy}"}
    )
    assert response.status_code == 401


async def test_a_gateway_inference_grant_is_not_accepted_here(client):
    """An ``agr`` grant authorizes models at the gateway and nothing here.

    This service cannot verify one -- it holds none of the gateway's signing
    material -- so the refusal is structural rather than a policy. Asserted
    anyway: "it happens not to parse" and "we refuse it" fail differently when
    somebody later adds a gateway client to this module.
    """
    response = await client.post(
        _MCP, json=_rpc("tools/list"), headers={"Authorization": "Bearer agr.cGF5bG9hZA.c2ln"}
    )
    assert response.status_code in (401, 403)


async def test_mcp_is_available_without_a_rollout_flag(client, monkeypatch):
    """Legacy environment values cannot disable the shipped MCP surface."""
    monkeypatch.setenv("AGENT_MCP_ENABLED", "0")
    response = await client.post(_MCP, json=_rpc("tools/list"), headers=_auth())
    assert response.status_code == 200
    assert "result" in response.json()
