"""Focused request-contract tests for the private Session Supervisor client."""

from __future__ import annotations

import json

import httpx
import pytest

from cloud_agent_host.chat_sandbox import ChatSandboxSpec
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
)


def _spec() -> ChatSandboxSpec:
    return ChatSandboxSpec(
        thread_id="athr_origin",
        sandbox_id="asbx_shared",
        generation=3,
        runtime_id="claude-code",
        runtime_version="2.1.220",
        sandbox_image_digest="sha256:image",
        resident_slot_capacity=8,
        memory_limit="6g",
        cpu_limit="3",
        disk_limit_mib=16384,
        stop_timeout_s=20,
    )


def _client(handler) -> SessionSupervisorClient:
    client = SessionSupervisorClient("http://broker", "broker-secret")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-Agent-Workspace-Token": "broker-secret"},
    )
    return client


def test_initial_archive_sends_the_exact_sandbox_identity_with_a_long_timeout():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"enabled": True, "stored": True})

    members = [{"thread_id": "athr_origin", "resident_slot_id": 0}]
    client = _client(handler)
    try:
        answer = client.capture_initial_archive(
            _spec(),
            thread_id="athr/current",
            token="lease-token",
            members=members,
            expected_container_id="container-shared",
            preparation_id="ajob_initial",
            base_sha="b" * 40,
        )
    finally:
        client.close()

    [request] = seen
    assert request.method == "POST"
    assert request.url.raw_path == b"/shared-sandboxes/athr%2Fcurrent/archive-initial"
    assert request.headers["X-Agent-Workspace-Token"] == "broker-secret"
    assert request.extensions["timeout"] == {
        "connect": 900.0,
        "read": 900.0,
        "write": 900.0,
        "pool": 900.0,
    }
    assert json.loads(request.content) == {
        "sandbox_id": "asbx_shared",
        "generation": 3,
        "origin_thread_id": "athr_origin",
        "runtime_id": "claude-code",
        "runtime_version": "2.1.220",
        "sandbox_image_digest": "sha256:image",
        "expected_container_id": "container-shared",
        "resident_slot_capacity": 8,
        "memory_limit": "6g",
        "cpu_limit": "3",
        "disk_limit_mib": 16384,
        "stop_timeout_s": 20,
        "members": members,
        "token": "lease-token",
        "preparation_id": "ajob_initial",
        "base_sha": "b" * 40,
    }
    assert answer == {"enabled": True, "stored": True}


def test_initial_archive_preserves_a_broker_refusal():
    client = _client(
        lambda _request: httpx.Response(409, json={"detail": "initial archive is too late"})
    )
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            client.capture_initial_archive(
                _spec(),
                thread_id="athr_origin",
                token="lease-token",
                members=[],
                expected_container_id="container-shared",
                preparation_id="ajob_initial",
                base_sha="b" * 40,
            )
    finally:
        client.close()

    assert raised.value.status_code == 409
    assert str(raised.value) == "initial archive is too late"


def test_initial_archive_status_runs_before_reset_with_the_exact_identity():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "enabled": True,
                "prepared": True,
                "stored": True,
                "base_sha": "b" * 40,
            },
        )

    members = [{"thread_id": "athr_origin", "resident_slot_id": 0}]
    client = _client(handler)
    try:
        answer = client.initial_archive_status(
            _spec(),
            thread_id="athr/current",
            token="lease-token",
            members=members,
            expected_container_id="container-shared",
            preparation_id="ajob_initial",
        )
    finally:
        client.close()

    [request] = seen
    assert request.url.raw_path == b"/shared-sandboxes/athr%2Fcurrent/archive-initial/status"
    payload = json.loads(request.content)
    assert payload == {
        "sandbox_id": "asbx_shared",
        "generation": 3,
        "origin_thread_id": "athr_origin",
        "runtime_id": "claude-code",
        "runtime_version": "2.1.220",
        "sandbox_image_digest": "sha256:image",
        "expected_container_id": "container-shared",
        "resident_slot_capacity": 8,
        "memory_limit": "6g",
        "cpu_limit": "3",
        "disk_limit_mib": 16384,
        "stop_timeout_s": 20,
        "members": members,
        "token": "lease-token",
        "preparation_id": "ajob_initial",
    }
    assert request.extensions["timeout"]["read"] == 900.0
    assert answer["base_sha"] == "b" * 40


def test_clean_stop_allows_time_for_synchronous_final_archive():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"running": False})

    client = _client(handler)
    try:
        client.stop_shared_idle("athr/current", generation=3, activation_generation=7)
    finally:
        client.close()

    [request] = seen
    assert request.url.raw_path == b"/shared-sandboxes/athr%2Fcurrent/stop-idle"
    assert json.loads(request.content) == {
        "generation": 3,
        "activation_generation": 7,
        "reason": "idle",
    }
    assert request.extensions["timeout"] == {
        "connect": 1800.0,
        "read": 1800.0,
        "write": 1800.0,
        "pool": 1800.0,
    }
