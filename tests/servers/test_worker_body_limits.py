"""Raw resource-report limits apply before parsing, auth and input rendering."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from cloud_agent.api import host_profiling, hosts, worker
from cloud_agent.api._body_limits import MAX_HOST_REQUEST_BODY_BYTES
from cloud_agent.api.deps import get_storage

pytestmark = pytest.mark.asyncio

ENDPOINT = "/v1/agent/worker/jobs/job-body-limit/turn-resources"


def _app():
    app = FastAPI()
    app.include_router(worker.router)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace()
    return app


async def test_unauthenticated_oversized_invalid_json_is_refused_before_parsing(monkeypatch):
    async def unexpected_json(self):
        raise AssertionError("an oversized request reached JSON parsing")

    monkeypatch.setattr(Request, "json", unexpected_json)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            ENDPOINT,
            content=b"invalid-json-sentinel" + b"x" * worker.MAX_TURN_RESOURCES_BODY_BYTES,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["error"]["type"] == "payload_too_large"
    assert response.headers["Cache-Control"] == "no-store"
    assert "invalid-json-sentinel" not in response.text


async def test_chunked_oversized_resource_body_stops_reading_at_the_limit(monkeypatch):
    delivered: list[int] = []

    async def chunks():
        for index in range(3):
            delivered.append(index)
            yield b"x" * (worker.MAX_TURN_RESOURCES_BODY_BYTES // 2 + 1)

    async def unexpected_json(self):
        raise AssertionError("an oversized chunked request reached JSON parsing")

    monkeypatch.setattr(Request, "json", unexpected_json)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            ENDPOINT, content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert response.request.headers.get("Content-Length") is None
    assert response.status_code == 413
    assert delivered == [0, 1]


@pytest.mark.parametrize("content", [b'{"bogus":"private-telemetry-sentinel"}', b"{malformed"])
async def test_invalid_resource_reports_have_bounded_validation_responses(content):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            ENDPOINT, content=content, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["type"] == "invalid_request"
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.content) < 200
    assert "private-telemetry-sentinel" not in response.text


async def test_a_valid_bounded_resource_report_still_requires_a_worker_credential():
    body = {
        "measurement_id": "measurement-test",
        "report_seq": 1,
        "state": "collecting",
        "window": {"duration_ms": 1, "observed_through_ms": 1},
        "cpu": {"status": "unavailable", "method": "live_and_exit_task_counters"},
        "memory": {
            "status": "unavailable",
            "method": "sampled_member_pss",
            "sample_interval_ms": 1000,
        },
        "storage_io": {"status": "unavailable", "method": "task_io_accounting"},
    }
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(ENDPOINT, json=body)
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["type"] == "unauthorized"


# ── The host ceiling on every other host-facing body ──────────────────

_OVERSIZED = b"oversized-body-sentinel" + b"x" * MAX_HOST_REQUEST_BODY_BYTES


def _host_app():
    app = FastAPI()
    app.include_router(worker.router)
    app.include_router(hosts.router)
    app.include_router(host_profiling.router)
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace()
    return app


@pytest.mark.parametrize(
    "path",
    [
        "/v1/agent/worker/jobs/job-body-limit/events",
        "/v1/agent/worker/jobs/job-body-limit/finish",
        "/v1/agent/worker/claim",
        "/v1/agent/host/heartbeat",
        "/v1/agent/host/register",
        "/v1/agent/hosts/profiling-policy",
    ],
)
async def test_an_oversized_host_body_is_refused_before_parsing_or_auth(monkeypatch, path):
    """Hosts reach these through the tunnel, where no edge limit applies."""

    async def unexpected_json(self):
        raise AssertionError("an oversized host request reached JSON parsing")

    monkeypatch.setattr(Request, "json", unexpected_json)
    async with AsyncClient(
        transport=ASGITransport(app=_host_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            path, content=_OVERSIZED, headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413
    assert response.json()["detail"]["error"]["type"] == "payload_too_large"
    assert response.headers["Cache-Control"] == "no-store"
    assert "oversized-body-sentinel" not in response.text


async def test_a_chunked_oversized_host_body_stops_reading_at_the_limit():
    delivered: list[int] = []

    async def chunks():
        for index in range(3):
            delivered.append(index)
            yield b"x" * (MAX_HOST_REQUEST_BODY_BYTES // 2 + 1)

    async with AsyncClient(
        transport=ASGITransport(app=_host_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/host/heartbeat",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )
    assert response.request.headers.get("Content-Length") is None
    assert response.status_code == 413
    assert delivered == [0, 1]


async def test_an_ordinary_host_body_still_reaches_authentication():
    """The ceiling is a ceiling: a normal beat is parsed and then authenticated."""
    async with AsyncClient(
        transport=ASGITransport(app=_host_app()), base_url="http://test"
    ) as client:
        heartbeat = await client.post("/v1/agent/host/heartbeat", json={"slots_total": 2})
        events = await client.post(
            "/v1/agent/worker/jobs/job-body-limit/events",
            json={"event_type": "message", "payload": {"text": "x" * 200_000}},
        )
    assert heartbeat.status_code == 401
    assert events.status_code == 401
