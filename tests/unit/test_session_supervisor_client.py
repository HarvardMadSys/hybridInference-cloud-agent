"""Focused request-contract tests for the private Session Supervisor client."""

from __future__ import annotations

import json
from types import SimpleNamespace

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
    client = SessionSupervisorClient("http://sandbox_manager", "sandbox-manager-secret")
    client._client.close()
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
    )
    return client


_SSH_ACCESS = SimpleNamespace(model_dump=lambda **_kwargs: {"revision": 7})

_ORDINARY_REQUESTS = [
    pytest.param(
        lambda client: client.interrupt_shared("athr/request", token="lease-token"),
        "shared Session Supervisor is unavailable",
        "shared Session Supervisor returned invalid data",
        "POST",
        b"/shared-sandboxes/athr%2Frequest/interrupt",
        5.0,
        id="shared-lifecycle",
    ),
    pytest.param(
        lambda client: client.apply_profiling_policy({"revision": 7}),
        "sandbox manager is unavailable",
        "sandbox manager returned invalid data",
        "POST",
        b"/profiling-policy",
        5.0,
        id="profiling-policy",
    ),
    pytest.param(
        lambda client: client.apply_host_settings({"terminal_max_sessions": 16}),
        "sandbox manager is unavailable",
        "sandbox manager returned invalid data",
        "POST",
        b"/host-settings",
        5.0,
        id="host-settings",
    ),
    pytest.param(
        lambda client: client.apply_sandbox_archive_settings({"enabled": True}),
        "sandbox manager is unavailable",
        "sandbox manager returned invalid data",
        "POST",
        b"/sandbox-archive-settings",
        5.0,
        id="archive-settings",
    ),
    pytest.param(
        lambda client: client.list(),
        "chat Session Supervisor is unavailable",
        "chat Session Supervisor returned invalid data",
        "GET",
        b"/sandboxes",
        5.0,
        id="inventory",
    ),
    pytest.param(
        lambda client: client.environment_snapshot_images(),
        "sandbox manager is unavailable",
        "sandbox manager returned invalid data",
        "POST",
        b"/environment-snapshots/images",
        150.0,
        id="manager-post",
    ),
    pytest.param(
        lambda client: client.sync_ssh_access("athr/request", _SSH_ACCESS, starting=True),
        "sandbox manager is unavailable",
        "SSH access returned invalid data",
        "PUT",
        b"/workspaces/athr%2Frequest/ssh-access",
        120.0,
        id="ssh-access",
    ),
]


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
    assert request.headers["X-Agent-Workspace-Token"] == "sandbox-manager-secret"
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


def test_initial_archive_preserves_a_sandbox_manager_refusal():
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


@pytest.mark.parametrize(
    ("call", "unavailable", "invalid", "method", "raw_path", "read_timeout"),
    _ORDINARY_REQUESTS,
)
def test_ordinary_requests_preserve_method_path_and_timeout(
    call,
    unavailable: str,
    invalid: str,
    method: str,
    raw_path: bytes,
    read_timeout: float,
):
    del unavailable, invalid
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.raw_path == b"/sandboxes":
            return httpx.Response(200, json={"sandboxes": []})
        if request.url.raw_path == b"/environment-snapshots/images":
            return httpx.Response(200, json={"snapshot_ids": []})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    try:
        call(client)
    finally:
        client.close()

    [request] = seen
    assert request.method == method
    assert request.url.raw_path == raw_path
    assert request.extensions["timeout"]["read"] == read_timeout


@pytest.mark.parametrize(
    ("call", "unavailable", "invalid", "method", "raw_path", "read_timeout"),
    _ORDINARY_REQUESTS,
)
def test_ordinary_requests_normalize_transport_failures(
    call,
    unavailable: str,
    invalid: str,
    method: str,
    raw_path: bytes,
    read_timeout: float,
):
    del invalid, method, raw_path, read_timeout

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = _client(handler)
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            call(client)
    finally:
        client.close()

    assert raised.value.status_code == 503
    assert str(raised.value) == unavailable
    assert isinstance(raised.value.__cause__, httpx.ConnectError)


@pytest.mark.parametrize(
    ("call", "unavailable", "invalid", "method", "raw_path", "read_timeout"),
    _ORDINARY_REQUESTS,
)
@pytest.mark.parametrize(
    ("body", "malformed"),
    [
        pytest.param(b"{", True, id="malformed-json"),
        pytest.param(b"[]", False, id="non-object-json"),
    ],
)
def test_ordinary_success_requires_a_json_object(
    call,
    unavailable: str,
    invalid: str,
    method: str,
    raw_path: bytes,
    read_timeout: float,
    body: bytes,
    malformed: bool,
):
    del unavailable, method, raw_path, read_timeout
    client = _client(
        lambda _request: httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json"},
        )
    )
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            call(client)
    finally:
        client.close()

    assert raised.value.status_code == 502
    assert str(raised.value) == invalid
    if malformed:
        assert isinstance(raised.value.__cause__, ValueError)
    else:
        assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("call", "response", "expected"),
    [
        pytest.param(
            lambda client: client.apply_profiling_policy({"revision": 7}),
            httpx.Response(422, text="not JSON"),
            ("sandbox manager rejected the profiling policy: HTTP 422 Unprocessable Entity"),
            id="plain-text",
        ),
        pytest.param(
            lambda client: client.apply_sandbox_archive_settings({"enabled": True}),
            httpx.Response(
                422,
                json={
                    "detail": [
                        {
                            "loc": ["body", "settings", "credential"],
                            "msg": "String should have at most 8 characters",
                            "input": "super-secret",
                        }
                    ]
                },
            ),
            (
                "sandbox manager rejected sandbox archive settings: "
                "HTTP 422 Unprocessable Entity "
                "(settings.credential: String should have at most 8 characters)"
            ),
            id="validation-list",
        ),
        pytest.param(
            lambda client: client.apply_host_settings({"terminal_max_sessions": 16}),
            httpx.Response(409, json={"detail": "runtime settings revision is stale"}),
            "runtime settings revision is stale",
            id="string-detail",
        ),
    ],
)
def test_ordinary_refusals_use_the_existing_detail_policy(call, response, expected: str):
    client = _client(lambda _request: response)
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            call(client)
    finally:
        client.close()

    assert raised.value.status_code == response.status_code
    assert str(raised.value) == expected
    assert "super-secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("call", "body", "expected"),
    [
        pytest.param(
            lambda client: client.list(),
            {"sandboxes": [{}, "not-an-object"]},
            "chat Session Supervisor returned invalid data",
            id="inventory-rows",
        ),
        pytest.param(
            lambda client: client.environment_snapshot_images(),
            {"snapshot_ids": "not-a-list"},
            "sandbox manager returned invalid data",
            id="snapshot-image-ids",
        ),
    ],
)
def test_endpoint_specific_fields_remain_validated_after_object_decoding(call, body, expected: str):
    client = _client(lambda _request: httpx.Response(200, json=body))
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            call(client)
    finally:
        client.close()

    assert raised.value.status_code == 502
    assert str(raised.value) == expected
