"""Contract smoke tests for the loopback-only development gateway."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
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
    gateway._concurrency_probes.clear()
    gateway._completed_concurrency_probes.clear()
    monkeypatch.setenv("LOCAL_GATEWAY_DISPATCH_TOKEN", "local-dispatch")
    monkeypatch.setenv("LOCAL_IDENTITY_ALLOWED_REDIRECTS", "http://127.0.0.1:13002/callback")


def test_default_identity_redirects_follow_the_configured_edge_port(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_IDENTITY_ALLOWED_REDIRECTS")
    monkeypatch.setenv("LOCAL_EDGE_PORT", "23002")

    assert gateway._allowed_redirects() == {
        "http://127.0.0.1:23002/api/v1/session/callback",
        "http://localhost:23002/api/v1/session/callback",
    }


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
        access = await client.get(
            f"/internal/users/{gateway.USER_ID}/agent-access", headers=internal
        )
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
    assert set(claims) == {"sub", "iss", "aud", "iat", "exp"}
    assert access.json() == {
        "user_id": gateway.USER_ID,
        "allowed": True,
        "permissions": ["agent.use", "agent.admin"],
    }
    assert catalog.json()["models"] == [gateway.MODEL_ID]
    assert minted.json()["allowed_models"] == [gateway.MODEL_ID]
    assert "event: message_stop" in inference.text


async def test_responses_stream_completes_the_assistant_item_for_native_history() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=gateway.app), base_url="http://local"
    ) as client:
        minted = await client.post(
            "/internal/agent-grants",
            headers={"Authorization": "Bearer local-dispatch"},
            json={
                "user_id": gateway.USER_ID,
                "external_job_id": "native-history-fixture",
                "external_attempt_id": "1",
                "allowed_models": [gateway.MODEL_ID],
            },
        )
        response = await client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer " + minted.json()["token"]},
            json={
                "model": gateway.MODEL_ID,
                "stream": True,
                "input": [{"role": "user", "content": "hi"}],
            },
        )
    assert response.status_code == 200
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    kinds = [event["type"] for event in events]
    assert "response.output_item.added" in kinds
    assert "response.output_item.done" in kinds
    assert kinds.index("response.output_item.added") < kinds.index("response.output_text.delta")
    assert kinds.index("response.output_text.done") < kinds.index("response.output_item.done")
    assert kinds.index("response.output_item.done") < kinds.index("response.completed")
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    done = events[kinds.index("response.output_item.done")]["item"]
    completed = events[kinds.index("response.completed")]["response"]
    assert done == completed["output"][0]
    assert done["role"] == "assistant" and done["status"] == "completed"
    assert done["content"][0]["text"] == "".join(
        event["delta"] for event in events if event["type"] == "response.output_text.delta"
    )


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


async def test_concurrent_fork_probe_requires_two_current_inputs(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "CONCURRENT_FORK_PROBE_WAIT_S", 0.1)
    monkeypatch.setattr(gateway, "CONCURRENT_FORK_PROBE_HOLD_S", 0.001)
    await gateway._hold_concurrent_fork_probe({"input": "ordinary turn"})
    first = asyncio.create_task(
        gateway._hold_concurrent_fork_probe({"input": "source [local-concurrency-probe:codex]"})
    )
    await asyncio.sleep(0)
    assert not first.done()
    second = asyncio.create_task(
        gateway._hold_concurrent_fork_probe({"input": "child [local-concurrency-probe:codex]"})
    )

    await asyncio.gather(first, second)

    assert gateway._concurrency_probes == {}
    assert set(gateway._completed_concurrency_probes) == {"codex"}


async def test_concurrent_fork_probe_ignores_a_marker_only_in_history(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "CONCURRENT_FORK_PROBE_WAIT_S", 0.001)

    await gateway._hold_concurrent_fork_probe(
        {
            "messages": [
                {"role": "user", "content": "old [local-concurrency-probe:codex]"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "ordinary new turn"},
            ]
        }
    )

    assert gateway._concurrency_probes == {}
    assert not gateway._completed_concurrency_probes


async def test_concurrent_fork_probe_fails_a_serialized_single_arrival(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "CONCURRENT_FORK_PROBE_WAIT_S", 0.001)

    with pytest.raises(gateway.ContractError) as captured:
        await gateway._hold_concurrent_fork_probe(
            {"input": "source [local-concurrency-probe:serialized]"}
        )

    assert captured.value.status == 400
    assert captured.value.kind == "concurrency_probe_timeout"
    assert gateway._concurrency_probes == {}
    assert not gateway._completed_concurrency_probes
