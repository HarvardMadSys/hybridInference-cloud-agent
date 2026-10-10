"""The public node-registration endpoint, with the real command service.

A node calls it with nothing but its one-time token. Every answer must be the
same for any token that is not the open one, and no response may echo it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import compute_registration
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.compute_config import ComputeConfiguration
from cloud_agent.host_pool import hash_secret

TOKEN = "node-token-" + "x" * 40
KEY = (
    ed25519.Ed25519PrivateKey.generate()
    .public_key()
    .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    .decode()
)
PATH = "/v1/compute/node-registrations"


class Registrations:
    """Registration storage that answers as the repository would."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def complete_node_registration(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.outcome


def client_for(outcome: str | None) -> tuple[AsyncClient, Registrations]:
    registrations = Registrations(outcome or "invalid")
    app = FastAPI()
    if outcome is not None:
        app.state.compute_commands = ComputeCommands(
            repository=registrations,  # type: ignore[arg-type]
            registry=SimpleNamespace(),  # type: ignore[arg-type]
            secrets=SimpleNamespace(),  # type: ignore[arg-type]
            configuration=ComputeConfiguration(),
        )
    app.include_router(compute_registration.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test"), registrations


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["registered", "replayed"])
async def test_a_node_registers_its_keys_with_its_token(outcome):
    client, registrations = client_for(outcome)
    async with client:
        response = await client.post(
            PATH, json={"token": TOKEN, "host_keys": [KEY + " root@node0"]}
        )
    assert response.status_code == 200
    assert response.json() == {"registered": True}
    assert response.headers["cache-control"] == "no-store"
    assert registrations.calls == [{"token_hash": hash_secret(TOKEN), "host_keys": [KEY]}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,status,code",
    [("conflict", 409, "registration_conflict"), ("invalid", 404, "registration_not_found")],
)
async def test_refusals_carry_a_code_and_never_the_token(outcome, status, code):
    client, _ = client_for(outcome)
    async with client:
        response = await client.post(PATH, json={"token": TOKEN, "host_keys": [KEY]})
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code
    assert TOKEN not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"token": TOKEN},
        {"token": TOKEN, "host_keys": []},
        {"token": TOKEN, "host_keys": [KEY], "allocation_id": "someone-else"},
        {"token": TOKEN, "host_keys": ["ssh-ed25519 not-base64!"]},
    ],
)
async def test_malformed_requests_are_refused_without_echoing_the_token(body):
    client, registrations = client_for("registered")
    async with client:
        response = await client.post(PATH, json=body)
    assert response.status_code == 422
    assert TOKEN not in response.text
    assert registrations.calls == []


@pytest.mark.asyncio
async def test_without_compute_management_the_endpoint_is_unavailable():
    client, _ = client_for(None)
    async with client:
        response = await client.post(PATH, json={"token": TOKEN, "host_keys": [KEY]})
    assert response.status_code == 503
