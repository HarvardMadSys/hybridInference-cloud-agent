"""What the code exchange puts on the wire is what the gateway declares.

Written after the same drift was found twice elsewhere: `GrantClient.mint`
sent a field the gateway had stopped declaring, and the lookup contract
document declared an endpoint the gateway had stopped serving. Each repository
tested its own half against its own fake, and the halves disagreed for as long
as nobody compared them.

This one matters more than most, because the exchange has no second chance: the
gateway consumes the authorization code *before* it validates the body, so a
request with a field renamed does not fail cleanly and retry — it burns the
code and the user is bounced back to the start of a login that will fail the
same way next time.

**What this cannot do.** The contract file is a transcription of the gateway's
router, so a client and a document stale in the same direction still pass.
Running the flow against a real gateway is what catches that; the local stack
(`deploy/local`) is where that happens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from cloud_agent import identity
from cloud_agent.gateway import GatewayClient

pytestmark = pytest.mark.asyncio

CONTRACT = Path(__file__).resolve().parents[2] / "gateway_contracts" / "identity.openapi.yaml"


def _schema(path: str, method: str) -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    body = document["paths"][path][method]["requestBody"]
    return body["content"]["application/json"]["schema"]


class _CapturingClient:
    """An httpx-shaped stub that records the one call it is given."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: Any = None) -> Any:
        self.calls.append({"url": url, "json": json})

        class _Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, Any]:
                return {"access_token": "an.identity.token", "token_type": "Bearer"}

        return _Response()


async def test_the_exchange_sends_exactly_the_declared_fields():
    """No missing field, and no extra one either.

    An extra field is not harmless: the gateway models this body with a strict
    schema, and a request it cannot parse is one that already cost a code.
    """
    stub = _CapturingClient()
    client = GatewayClient(base_url="https://gateway.example", client=stub)

    token = await client.exchange_authorization_code(
        code="the-code",
        verifier=identity.new_verifier(),
        client_id="cloud-agent",
        redirect_uri="https://agents.example/api/v1/session/callback",
    )

    assert token == "an.identity.token"
    assert stub.calls[0]["url"] == "https://gateway.example/v1/identity/token"
    declared = _schema("/v1/identity/token", "post")
    assert set(stub.calls[0]["json"]) == set(declared["required"])
    assert set(stub.calls[0]["json"]) <= set(declared["properties"])


async def test_the_exchange_carries_no_service_credential():
    """PKCE authenticates it, and a dispatch token here would change what it means.

    Sending one would make redeeming a code something this *deployment* can
    do rather than something the holder of the verifier can — which is the
    property the whole flow rests on.
    """
    stub = _CapturingClient()
    client = GatewayClient(
        base_url="https://gateway.example", dispatch_token="secret-token", client=stub
    )

    await client.exchange_authorization_code(
        code="the-code",
        verifier=identity.new_verifier(),
        client_id="cloud-agent",
        redirect_uri="https://agents.example/api/v1/session/callback",
    )

    assert "headers" not in stub.calls[0]
    assert "secret-token" not in str(stub.calls[0])


async def test_the_verifier_and_challenge_fit_what_the_gateway_accepts():
    """Length bounds are part of the contract, and PKCE has tight ones.

    RFC 7636 puts a verifier between 43 and 128 characters, and both gateway
    endpoints model that range. A verifier one character short is refused at
    the exchange — after the code has been consumed.
    """
    verifier = identity.new_verifier()
    declared = _schema("/v1/identity/token", "post")["properties"]["code_verifier"]
    assert declared["minLength"] <= len(verifier) <= declared["maxLength"]

    challenge = identity.challenge_for(verifier)
    code_request = _schema("/v1/identity/code", "post")["properties"]["code_challenge"]
    assert code_request["minLength"] <= len(challenge) <= code_request["maxLength"]

    # base64url, unpadded: a `+`, `/` or `=` here is a challenge the gateway
    # decodes to different bytes than the ones that were hashed.
    assert all(character.isalnum() or character in "-_" for character in challenge)
