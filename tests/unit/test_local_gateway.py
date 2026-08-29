"""Contract smoke tests for the loopback-only development gateway."""

from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from httpx import ASGITransport, AsyncClient

from deploy.local import gateway


@pytest.fixture(autouse=True)
def local_gateway_state(monkeypatch):
    gateway._codes.clear()
    gateway._grants.clear()
    gateway._grant_tokens.clear()
    monkeypatch.setenv("LOCAL_GATEWAY_DISPATCH_TOKEN", "local-dispatch")
    monkeypatch.setenv("LOCAL_IDENTITY_ALLOWED_REDIRECTS", "http://127.0.0.1:13002/callback")


async def test_pkce_identity_internal_contracts_and_inference_grant() -> None:
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "client_id": "cloud-agent",
        "redirect_uri": "http://127.0.0.1:13002/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-1",
    }
    internal = {"Authorization": "Bearer local-dispatch"}

    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://local", follow_redirects=False
    ) as client:
        consent = await client.get("/authorize", params=params)
        continued = await client.get("/authorize/continue", params=params)
        callback = parse_qs(urlparse(continued.headers["location"]).query)
        token = await client.post(
            "/v1/identity/token",
            json={
                "code": callback["code"][0],
                "code_verifier": verifier,
                "client_id": "cloud-agent",
                "redirect_uri": params["redirect_uri"],
            },
        )
        jwks = (await client.get("/v1/identity/jwks")).json()
        claims = jwt.decode(
            token.json()["access_token"],
            jwt.PyJWK.from_dict(jwks["keys"][0]).key,
            algorithms=["RS256"],
            audience="cloud-agent",
        )
        status = await client.get(f"/internal/users/{gateway.USER_ID}/status", headers=internal)
        catalog = await client.get(
            "/internal/model-catalog", params={"user_id": gateway.USER_ID}, headers=internal
        )
        minted = await client.post(
            "/internal/agent-grants",
            headers=internal,
            json={
                "user_id": gateway.USER_ID,
                "external_job_id": "job-1",
                "external_attempt_id": "1",
                "allowed_models": [gateway.MODEL_ID],
            },
        )
        inference = await client.post(
            "/v1/messages",
            headers={"x-api-key": minted.json()["token"]},
            json={"model": gateway.MODEL_ID, "stream": True, "messages": []},
        )

    assert consent.status_code == 200
    assert "Continue locally" in consent.text
    assert continued.status_code == 302
    assert claims["sub"] == gateway.USER_ID
    assert status.json()["active"] is True
    assert catalog.json()["models"] == [gateway.MODEL_ID]
    assert minted.json()["allowed_models"] == [gateway.MODEL_ID]
    assert "event: message_stop" in inference.text


async def test_codes_are_one_time_and_dispatch_errors_keep_contract_shape() -> None:
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "client_id": "cloud-agent",
        "redirect_uri": "http://127.0.0.1:13002/callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-1",
    }
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://local", follow_redirects=False
    ) as client:
        continued = await client.get("/authorize/continue", params=params)
        code = parse_qs(urlparse(continued.headers["location"]).query)["code"][0]
        body = {
            "code": code,
            "code_verifier": verifier,
            "client_id": "cloud-agent",
            "redirect_uri": params["redirect_uri"],
        }
        first = await client.post("/v1/identity/token", json=body)
        replay = await client.post("/v1/identity/token", json=body)
        unauthorized = await client.get(
            "/internal/model-catalog", params={"user_id": gateway.USER_ID}
        )

    assert first.status_code == 200
    assert replay.status_code == 400
    assert replay.json()["error"]["type"] == "invalid_grant"
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["type"] == "unauthorized"
