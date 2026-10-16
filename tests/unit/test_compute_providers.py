"""Compute adapters exercise actual wire contracts without real accounts/resources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from cloud_agent.ports.external.compute_provider import (
    UNCHECKED_CLOUDLAB_HOST_KEY_REF,
    AcquireRequest,
    ExtendRequest,
    NodeRegistration,
    OperationIntent,
    ProviderError,
    ResourceRef,
    SshConnection,
    UnsupportedCapability,
)
from cloud_agent.providers.compute.cloudlab import CloudLabProvider
from cloud_agent.providers.compute.existing_ssh import ExistingSSHProvider
from cloud_agent.providers.compute.ssh import OpenSshTransport

EXPERIMENT = "f47eb2a4-a0ac-4656-80da-8a638d2d263c"
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
TOKEN = "private-account-token-never-in-errors"
CONFIG = {"credential_ref": "account-1", "project": "research", "external_id": EXPERIMENT}
SSH = {
    "address": "node.example.org",
    "username": "agent",
    "credential_ref": "ssh-1",
    "known_hosts_ref": "ssh-1:known_hosts",
}


def intent(acquisition: AcquireRequest | None = None, **kwargs: object) -> OperationIntent:
    return OperationIntent(
        operation_id=str(uuid4()),
        allocation_id=str(uuid4()),
        requested_at=NOW,
        request_fingerprint="test-fingerprint",
        acquisition=acquisition,
        **kwargs,
    )


def resource(**updates: object) -> dict[str, object]:
    return {
        "id": EXPERIMENT,
        "project": "research",
        "status": "ready",
        "expires_at": (NOW + timedelta(hours=4)).isoformat(),
        "aggregates": {
            "urn:aggregate": {
                "nodes": [
                    {
                        "urn": "urn:node:1",
                        "client_id": "vm1",
                        "hostname": "node.example.org",
                        "status": "ready",
                    }
                ]
            }
        },
        **updates,
    }


def openssh_user_key() -> str:
    return (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )


def _profile_list() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "profiles": [
                {"name": "one-node", "project": "PortalProfiles"},
                {"name": "small-lan", "project": "PortalProfiles"},
                {"name": "cloud-agent-node", "project": "research"},
            ]
        },
    )


def provider(handler: object) -> CloudLabProvider:
    resolver = AsyncMock()
    resolver.resolve.return_value = TOKEN
    return CloudLabProvider(
        api_url="https://cloudlab-api.example.org/v1",
        resolver=resolver,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_existing_ssh_acquisition_persists_identity_but_no_cloud_capabilities():
    transport = AsyncMock()
    transport.run.return_value = "f" * 32 + "\n"
    adapter = ExistingSSHProvider(transport)
    receipt = await adapter.acquire(AcquireRequest(mode="attach", config=SSH), intent())
    assert receipt.observation.lease_known is False
    assert receipt.observation.expires_at is None
    assert receipt.observation.nodes[0].machine_identity == hashlib.sha256(b"f" * 32).hexdigest()
    assert (
        receipt.resource.config["expected_machine_identity"]
        == receipt.observation.nodes[0].machine_identity
    )
    transport.run.return_value = "a" * 32
    with pytest.raises(ProviderError, match="identity changed"):
        await adapter.inspect(receipt.resource)
    for action in (
        adapter.extend(receipt.resource, ExtendRequest(target_expires_at=NOW, reason="research")),
        adapter.release(receipt.resource, intent()),
    ):
        with pytest.raises(UnsupportedCapability):
            await action
    with pytest.raises(UnsupportedCapability):
        adapter.validate_config("provision", SSH)


@pytest.mark.asyncio
async def test_existing_ssh_recovery_reattaches_without_installation():
    transport = AsyncMock()
    transport.run.return_value = "f" * 32
    request = AcquireRequest(mode="attach", config=SSH)
    operation = intent(request)
    recovered = await ExistingSSHProvider(transport).recover_acquire(operation)
    assert recovered.receipt.resource.external_id == operation.allocation_id
    assert transport.run.await_count == 1
    assert transport.run.call_args.args[1] == "cat /etc/machine-id"


def test_ssh_rejects_option_and_shell_injection():
    for field, value in [
        ("address", "-oProxyCommand=evil"),
        ("address", "node;touch /tmp/pwn"),
        ("username", "user@host"),
        ("port", 0),
    ]:
        with pytest.raises(ValidationError):
            SshConnection.model_validate({**SSH, field: value})


def test_cloudlab_requires_verified_https_endpoint_and_explicit_modes():
    for url in [
        "http://boss.emulab.net:43795",
        "https://user:token@example.org",
        "https://example.org?token=x",
        "https://example.org/#fragment",
    ]:
        with pytest.raises(ValueError):
            CloudLabProvider(api_url=url, resolver=AsyncMock())
    adapter = provider(lambda request: httpx.Response(200, json=resource()))
    with pytest.raises(ProviderError):
        adapter.validate_config("provision", CONFIG)
    with pytest.raises(ProviderError):
        adapter.validate_config("attach", {"credential_ref": "id", "project": "research"})
    with pytest.raises(ValidationError):
        adapter.validate_config("attach", {**CONFIG, "external_id": "../../tokens"})


@pytest.mark.asyncio
async def test_cloudlab_attach_collects_full_node_scope_and_manifest_vm_ssh_port():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["X-Api-Token"] == TOKEN
        if request.url.path.endswith("/manifests"):
            return httpx.Response(
                200,
                json={
                    "urn:aggregate": '<rspec xmlns="http://www.geni.net/resources/rspec/3"><node client_id="vm1"><services><login authentication="ssh-keys" username="agent" hostname="node.example.org" port="2207"/></services></node></rspec>'
                },
            )
        return httpx.Response(200, json=resource())

    adapter = provider(respond)
    config = {
        **CONFIG,
        "ssh_username": "agent",
        "ssh_credential_ref": "ssh-1",
        "ssh_known_hosts_ref": "ssh-1:known_hosts",
    }
    receipt = await adapter.acquire(AcquireRequest(mode="attach", config=config), intent())
    assert receipt.outcome == "confirmed"
    assert receipt.observation.scope_complete
    connection = receipt.observation.nodes[0].connection
    assert connection.port == 2207
    assert connection.known_hosts_ref == "ssh-1:known_hosts"
    assert connection.verify_host_key and "verify_host_key" not in connection.model_dump(
        mode="json"
    )
    assert (
        receipt.observation.nodes[0].instance_identity
        == EXPERIMENT + ":urn:aggregate:vm1:urn:node:1"
    )
    assert all(request.method == "GET" for request in requests)
    assert TOKEN not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_cloudlab_explicitly_allows_ssh_without_host_key_verification():
    def respond(request):
        if request.url.path.endswith("/manifests"):
            return httpx.Response(
                200,
                json={
                    "urn:aggregate": '<rspec xmlns="http://www.geni.net/resources/rspec/3"><node client_id="vm1"><services><login authentication="ssh-keys" username="agent" hostname="node.example.org" port="2207"/></services></node></rspec>'
                },
            )
        return httpx.Response(200, json=resource())

    observation = await provider(respond).inspect(
        ResourceRef(
            external_id=EXPERIMENT,
            config={
                **CONFIG,
                "ssh_username": "agent",
                "ssh_credential_ref": "ssh-1",
                "host_key_source": "known_hosts",
                "ssh_known_hosts_ref": UNCHECKED_CLOUDLAB_HOST_KEY_REF,
            },
        )
    )
    connection = observation.nodes[0].connection
    assert connection.credential_ref == "ssh-1"
    assert connection.port == 2207
    assert connection.known_hosts_ref == UNCHECKED_CLOUDLAB_HOST_KEY_REF
    assert connection.host_key is None
    assert connection.verify_host_key is False
    assert "verify_host_key" not in connection.model_dump(mode="json")


@pytest.mark.asyncio
async def test_cloudlab_observe_import_needs_no_ssh_or_manifest():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=resource())

    observation = await provider(respond).inspect(
        ResourceRef(external_id=EXPERIMENT, config=CONFIG)
    )
    assert observation.scope_complete
    assert observation.nodes[0].connection is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cloudlab_validation_uses_canonical_collection_without_redirect():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v1/experiments":
            return httpx.Response(307, headers={"Location": "/v1/experiments/"})
        assert request.url.path == "/v1/experiments/"
        return httpx.Response(200, json={"experiments": []})

    await provider(respond).validate_connection("account-1")
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].headers["X-Api-Token"] == TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["a", "a" * 16, "a" * 17])
async def test_cloudlab_checks_experiment_name_limit_before_posting(name):
    requests = []

    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        requests.append(request)
        return httpx.Response(201, json={"id": EXPERIMENT})

    adapter = provider(respond)
    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "name": name,
            "profile_name": "one-node",
            "profile_project": "PortalProfiles",
        },
    )
    if len(name) > 16:
        with pytest.raises(ValidationError):
            await adapter.acquire(request, intent())
        assert requests == []
    else:
        receipt = await adapter.acquire(request, intent())
        assert receipt.resource.external_id == EXPERIMENT
        assert json.loads(requests[0].content)["name"] == name


def _provision(name: str | None = None) -> dict[str, object]:
    config = {
        "credential_ref": "account-1",
        "project": "research",
        "profile_name": "one-node",
        "profile_project": "PortalProfiles",
    }
    if name is not None:
        config["name"] = name
    return config


def _routes(responses: dict[tuple[str, str], httpx.Response]):
    def respond(request):
        key = (request.method, request.url.path)
        if key not in responses:
            raise AssertionError(key)
        return responses[key]

    return respond


@pytest.mark.asyncio
async def test_cloudlab_missing_profile_is_rejected_before_create():
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"profiles": []})

    with pytest.raises(ProviderError, match="has no profile one-node") as caught:
        await provider(respond).acquire(
            AcquireRequest(mode="provision", config=_provision()), intent()
        )
    assert caught.value.code == "request_rejected"
    assert requests == [("GET", "/v1/profiles/")]


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_experiment", [False, True])
async def test_cloudlab_server_error_is_unknown_without_name_based_adoption(matching_experiment):
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/profiles/"):
            return httpx.Response(
                200, json={"profiles": [{"name": "one-node", "project": "PortalProfiles"}]}
            )
        if request.method == "POST":
            return httpx.Response(500, json={"error": "Internal server error", "code": 500})
        if matching_experiment:
            return httpx.Response(
                200,
                json={
                    "experiments": [{"id": EXPERIMENT, "name": "probe-exp", "project": "research"}]
                },
            )
        return httpx.Response(200, json={"experiments": []})

    receipt = await provider(respond).acquire(
        AcquireRequest(mode="provision", config=_provision("probe-exp")), intent()
    )

    assert receipt.outcome == "unknown"
    assert requests == [("GET", "/v1/profiles/"), ("POST", "/v1/experiments/")]


@pytest.mark.asyncio
async def test_cloudlab_provision_retains_returned_uuid_and_matches_create_contract():
    requests = []

    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        requests.append(request)
        if request.url.path == "/v1/experiments":
            return httpx.Response(307, headers={"Location": "/v1/experiments/"})
        return httpx.Response(201, json={"id": EXPERIMENT})

    adapter = provider(respond)
    config = {
        "credential_ref": "account-1",
        "project": "research",
        "profile_name": "one-node",
        "profile_project": "PortalProfiles",
        "duration": 6,
        "bindings": {"hardware": "c220g2"},
    }
    receipt = await adapter.acquire(AcquireRequest(mode="provision", config=config), intent())
    assert receipt.resource.external_id == EXPERIMENT
    assert receipt.outcome == "pending"
    payload = json.loads(requests[0].content)
    assert len(payload["name"]) == 16
    assert payload["name"].startswith("ca-")
    assert payload["duration"] == 6
    assert payload["profile_project"] == "PortalProfiles"
    assert payload["bindings"] == {"hardware": "c220g2"}
    assert "credential_ref" not in payload and "ssh_credential_ref" not in payload
    assert "sshpubkey" not in payload
    assert requests[0].url.path == "/v1/experiments/"


@pytest.mark.parametrize(
    "updates",
    [
        {"node_types": ["r6615", "r6615"]},
        {"node_types": [f"type-{index}" for index in range(21)]},
        {"node_types": [""]},
        {"node_types": ["r6615/c6620"]},
        {"node_types": [" r6615"]},
        {"node_types": ["r" * 33]},
        {"node_type_binding": "hardware.type"},
        {"node_type_binding": ""},
    ],
)
def test_cloudlab_validates_the_preference_order_and_binding_identifier(updates):
    with pytest.raises(ValidationError):
        provider(lambda request: httpx.Response(201)).validate_config(
            "provision", {**_provision(), **updates}
        )


@pytest.mark.asyncio
async def test_cloudlab_tries_preferred_types_in_order_and_persists_the_selected_type():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return _profile_list()
        node_type = json.loads(request.content)["bindings"]["phystype"]
        return (
            httpx.Response(201, json={"id": EXPERIMENT})
            if node_type == "r650"
            else httpx.Response(429, json={"error": "Not enough free nodes of type r6615"})
        )

    config = {
        **_provision(),
        "node_types": ["r6615", "c6620", "r650"],
        "bindings": {"cluster": "specified-cluster", "useVMs": False},
    }
    receipt = await provider(respond).acquire(
        AcquireRequest(mode="provision", config=config), intent()
    )
    payloads = [json.loads(request.content) for request in requests if request.method == "POST"]
    assert [payload["bindings"]["phystype"] for payload in payloads] == [
        "r6615",
        "c6620",
        "r650",
    ]
    assert len({payload["name"] for payload in payloads}) == 1
    assert all(payload["bindings"]["cluster"] == "specified-cluster" for payload in payloads)
    assert all(payload["bindings"]["useVMs"] == "False" for payload in payloads)
    assert [request.method for request in requests] == ["GET", "POST", "POST", "POST"]
    assert receipt.outcome == "pending"
    assert receipt.resource.external_id == EXPERIMENT
    assert receipt.resource.config["bindings"] == {
        "cluster": "specified-cluster",
        "useVMs": False,
        "phystype": "r650",
    }
    assert "phystype" not in config["bindings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["phystype", "hardware"])
async def test_cloudlab_explicit_node_type_wins_over_preferences_and_is_not_replaced(binding):
    attempted = []

    def respond(request):
        if request.method == "GET":
            return _profile_list()
        attempted.append(json.loads(request.content)["bindings"])
        return httpx.Response(429, json={"error": "Insufficient physical nodes"})

    config = {
        **_provision(),
        "node_types": ["r6615", "c6620"],
        "node_type_binding": binding,
        "bindings": {binding: "r650"},
    }
    with pytest.raises(ProviderError) as caught:
        await provider(respond).acquire(AcquireRequest(mode="provision", config=config), intent())
    assert caught.value.code == "capacity_unavailable"
    assert attempted == [{binding: "r650"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_cloudlab_blank_node_type_binding_uses_preferences(blank):
    attempted = []

    def respond(request):
        if request.method == "GET":
            return _profile_list()
        attempted.append(json.loads(request.content)["bindings"])
        return httpx.Response(201, json={"id": EXPERIMENT})

    config = {
        **_provision(),
        "node_types": ["r6615", "c6620"],
        "node_type_binding": "hardware",
        "bindings": {"hardware": blank},
    }
    receipt = await provider(respond).acquire(
        AcquireRequest(mode="provision", config=config), intent()
    )
    assert attempted == [{"hardware": "r6615"}]
    assert receipt.resource.config["bindings"] == {"hardware": "r6615"}


@pytest.mark.asyncio
async def test_cloudlab_exhausted_preferences_remain_retryable_and_restart_at_the_top():
    attempted = []

    def respond(request):
        if request.method == "GET":
            return _profile_list()
        attempted.append(json.loads(request.content)["bindings"]["phystype"])
        return httpx.Response(429, json={"error": "Insufficient physical nodes"})

    adapter = provider(respond)
    request = AcquireRequest(
        mode="provision", config={**_provision(), "node_types": ["r6615", "c6620"]}
    )
    for _ in range(2):
        with pytest.raises(ProviderError) as caught:
            await adapter.acquire(request, intent())
        assert (caught.value.code, caught.value.retryable) == ("capacity_unavailable", True)
    assert attempted == ["r6615", "c6620", "r6615", "c6620"]


@pytest.mark.asyncio
async def test_cloudlab_empty_preferences_preserve_the_profiles_hardware_choice():
    attempted = []

    def respond(request):
        if request.method == "GET":
            return _profile_list()
        attempted.append(json.loads(request.content)["bindings"])
        return httpx.Response(201, json={"id": EXPERIMENT})

    receipt = await provider(respond).acquire(
        AcquireRequest(mode="provision", config={**_provision(), "node_types": []}), intent()
    )
    assert attempted == [{}]
    assert receipt.resource.config["bindings"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "diagnostic",
    [
        "Insufficient nodes of type r6615",
        "Not enough available physical nodes to instantiate this experiment",
        "Not enough free nodes of type r6615",
        "Insufficient resources at this site",
        "Resource capacity is exhausted",
    ],
)
async def test_cloudlab_server_capacity_error_does_not_blindly_try_another_node_type(diagnostic):
    attempted = []
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/profiles/"):
            return _profile_list()
        attempted.append(json.loads(request.content)["bindings"]["phystype"])
        return httpx.Response(500, json={"error": diagnostic})

    receipt = await provider(respond).acquire(
        AcquireRequest(mode="provision", config={**_provision(), "node_types": ["r6615", "c6620"]}),
        intent(),
    )
    assert receipt.outcome == "unknown"
    assert attempted == ["r6615"]
    assert requests == [("GET", "/v1/profiles/"), ("POST", "/v1/experiments/")]


@pytest.mark.asyncio
async def test_cloudlab_capacity_error_does_not_adopt_a_matching_name():
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/profiles/"):
            return _profile_list()
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "experiments": [{"id": EXPERIMENT, "name": "probe-exp", "project": "research"}]
                },
            )
        return httpx.Response(500, json={"error": "Insufficient physical nodes"})

    receipt = await provider(respond).acquire(
        AcquireRequest(
            mode="provision", config={**_provision("probe-exp"), "node_types": ["r6615", "c6620"]}
        ),
        intent(),
    )
    assert receipt.outcome == "unknown"
    assert requests == [("GET", "/v1/profiles/"), ("POST", "/v1/experiments/")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code",
    [
        (httpx.Response(401), "credential_invalid"),
        (httpx.Response(403), "permission_denied"),
        (httpx.Response(400), "request_rejected"),
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(429, json={"error": "Request rate limit reached"}), "rate_limited"),
        (httpx.Response(500, json={"error": "Internal server error"}), None),
        (httpx.Response(500, json={"error": "Invalid parameter phystype"}), None),
        (httpx.ConnectTimeout("handshake timed out"), "temporarily_unavailable"),
        (httpx.ReadTimeout("create timed out"), None),
        (httpx.Response(201, json={"error": "missing experiment ID"}), None),
        (httpx.Response(201, text="not JSON"), None),
    ],
)
async def test_cloudlab_never_falls_back_after_a_noncapacity_or_ambiguous_create(failure, code):
    attempted = []

    def respond(request):
        if request.url.path.endswith("/profiles/"):
            return _profile_list()
        if request.method == "GET":
            return httpx.Response(200, json={"experiments": []})
        attempted.append(json.loads(request.content)["bindings"]["phystype"])
        if isinstance(failure, httpx.RequestError):
            raise failure
        return failure

    request = AcquireRequest(
        mode="provision", config={**_provision(), "node_types": ["r6615", "c6620"]}
    )
    if code:
        with pytest.raises(ProviderError) as caught:
            await provider(respond).acquire(request, intent())
        assert caught.value.code == code
        if code == "rate_limited":
            assert caught.value.retryable is True
    else:
        assert (await provider(respond).acquire(request, intent())).outcome == "unknown"
    assert attempted == ["r6615"]


@pytest.mark.asyncio
async def test_cloudlab_capacity_error_with_unknown_creation_status_does_not_fall_back():
    attempted = []

    def respond(request):
        if request.url.path.endswith("/profiles/"):
            return _profile_list()
        if request.method == "GET":
            return httpx.Response(503)
        attempted.append(json.loads(request.content)["bindings"]["phystype"])
        return httpx.Response(500, json={"error": "Insufficient physical nodes"})

    receipt = await provider(respond).acquire(
        AcquireRequest(mode="provision", config={**_provision(), "node_types": ["r6615", "c6620"]}),
        intent(),
    )
    assert attempted == ["r6615"]
    assert receipt.outcome == "unknown"


@pytest.mark.asyncio
async def test_cloudlab_provision_installs_the_ssh_users_public_key():
    requests = []

    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        requests.append(request)
        return httpx.Response(201, json={"id": EXPERIMENT})

    public_key = openssh_user_key()
    adapter = provider(respond)
    config = {
        "credential_ref": "account-1",
        "project": "research",
        "profile_name": "one-node",
        "profile_project": "PortalProfiles",
        "ssh_username": "researcher",
        "ssh_credential_ref": "ssh-1",
        "ssh_known_hosts_ref": "ssh-1:known_hosts",
        "ssh_public_key": f"  {public_key}   researcher@lab  ",
    }
    receipt = await adapter.acquire(AcquireRequest(mode="provision", config=config), intent())
    payload = json.loads(requests[0].content)
    assert payload["sshpubkey"] == f"{public_key} researcher@lab"
    assert receipt.resource.config["ssh_public_key"] == payload["sshpubkey"]
    assert TOKEN not in requests[0].content.decode()


def test_cloudlab_refuses_a_private_key_in_place_of_the_ssh_user_key():
    private_key = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    )
    with pytest.raises(ValidationError):
        provider(lambda request: httpx.Response(201)).validate_config(
            "provision",
            {
                "credential_ref": "account-1",
                "project": "research",
                "profile_name": "one-node",
                "profile_project": "PortalProfiles",
                "ssh_public_key": private_key.decode(),
            },
        )


@pytest.mark.asyncio
async def test_create_timeout_recovery_never_reposts_or_adopts_a_name_collision():
    requests = []

    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        requests.append(request)
        raise httpx.ReadTimeout("secret " + TOKEN)

    adapter = provider(respond)
    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "name": "same-name",
            "profile_name": "one-node",
            "profile_project": "PortalProfiles",
        },
    )
    operation = intent(request)
    assert (await adapter.acquire(request, operation)).outcome == "unknown"
    assert (await adapter.recover_acquire(operation)).outcome == "unknown"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_known_uuid_recovers_create_by_exact_identity():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=resource())

    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "one-node",
            "profile_project": "PortalProfiles",
        },
    )
    result = await provider(respond).recover_acquire(intent(request, known_external_id=EXPERIMENT))
    assert result.outcome == "confirmed"
    assert requests[0].url.path.endswith(EXPERIMENT)
    assert requests[0].method == "GET"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "credential_invalid"),
        (403, "permission_denied"),
        (400, "request_rejected"),
        (503, "temporarily_unavailable"),
        (429, "rate_limited"),
        (302, "request_rejected"),
    ],
)
async def test_provider_error_diagnostics_never_expose_secrets_or_call_redirect(status, code):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            text=TOKEN,
            headers={
                "Location": "https://attacker.example/",
                "X-Request-ID": EXPERIMENT,
            },
        )

    with pytest.raises(ProviderError) as error:
        await provider(respond).inspect(ResourceRef(external_id=EXPERIMENT, config=CONFIG))
    assert error.value.code == code
    assert TOKEN not in str(error.value)
    assert TOKEN not in repr(error.value.diagnostics)
    assert error.value.diagnostics["http_status"] == status
    assert error.value.diagnostics["provider_operation"] == "experiments.read"
    assert error.value.diagnostics["external_id"] == EXPERIMENT
    assert error.value.diagnostics["provider_request_id"] == EXPERIMENT
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cloudlab_preserves_safe_actionable_http_400_diagnostics():
    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return httpx.Response(
                200,
                json={"profiles": [{"name": "cloud-agent-node", "project": "FAST25-AE"}]},
            )
        return httpx.Response(
            400,
            json={"error": "Invalid parameter phystype"},
            headers={"X-Request-ID": EXPERIMENT},
        )

    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "cloud-agent-node",
            "profile_project": "FAST25-AE",
            "bindings": {"cluster": "utah", "phystype": "r6615"},
        },
    )
    operation = intent(request)
    with pytest.raises(ProviderError) as error:
        await provider(respond).acquire(request, operation)

    assert error.value.code == "request_rejected"
    assert error.value.retryable is False
    assert error.value.diagnostics == {
        "provider_operation": "experiments.create",
        "http_status": 400,
        "provider_reason_code": "invalid_profile_binding",
        "provider_request_id": EXPERIMENT,
        "operation_id": operation.operation_id,
        "allocation_id": operation.allocation_id,
        "requested_project": "research",
        "requested_profile": "cloud-agent-node",
        "requested_profile_project": "FAST25-AE",
        "requested_site": "utah",
        "requested_hardware": "r6615",
    }
    assert TOKEN not in str(error.value) + repr(error.value.diagnostics)


@pytest.mark.asyncio
async def test_cloudlab_classifies_detail_validation_errors_without_retaining_input():
    adapter = provider(
        lambda request: httpx.Response(
            400,
            json={"detail": "Not a HASH reference"},
            headers={"X-Request-ID": EXPERIMENT},
        )
    )
    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "cloud-agent-node",
            "profile_project": "FAST25-AE",
            "bindings": {"enabled": True},
        },
    )

    with pytest.raises(ProviderError) as error:
        await adapter.acquire(request, intent(request))

    assert error.value.code == "request_rejected"
    assert error.value.diagnostics["provider_reason_code"] == "invalid_profile_binding"
    assert error.value.diagnostics["provider_request_id"] == EXPERIMENT
    assert TOKEN not in repr(error.value.diagnostics)


@pytest.mark.asyncio
async def test_cloudlab_reads_bounded_422_detail_and_keeps_it_out_of_diagnostics():
    adapter = provider(
        lambda request: httpx.Response(
            422,
            json={
                "detail": [{"loc": ["body", "bindings"], "msg": "invalid value", "input": TOKEN}]
            },
            headers={"X-Request-ID": EXPERIMENT},
        )
    )
    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "one-node",
            "profile_project": "PortalProfiles",
        },
    )

    with pytest.raises(ProviderError) as error:
        await adapter.acquire(request, intent(request))

    assert error.value.code == "request_rejected"
    assert error.value.diagnostics["http_status"] == 422
    assert error.value.diagnostics["provider_reason_code"] == "invalid_parameter"
    assert TOKEN not in str(error.value) + repr(error.value.diagnostics)


@pytest.mark.asyncio
async def test_cloudlab_successful_create_receipt_keeps_request_correlation():
    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        return httpx.Response(
            201,
            json={"id": EXPERIMENT},
            headers={"X-Request-ID": EXPERIMENT},
        )

    adapter = provider(respond)
    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "one-node",
            "profile_project": "PortalProfiles",
        },
    )

    operation = intent(request)
    receipt = await adapter.acquire(request, operation)

    assert receipt.outcome == "pending"
    assert receipt.resource is not None and receipt.resource.external_id == EXPERIMENT
    assert receipt.diagnostics == {
        "provider_operation": "experiments.create",
        "http_status": 201,
        "provider_reason_code": "request_accepted",
        "provider_request_id": EXPERIMENT,
        "operation_id": operation.operation_id,
        "allocation_id": operation.allocation_id,
        "external_id": EXPERIMENT,
        "requested_project": "research",
        "requested_profile": "one-node",
        "requested_profile_project": "PortalProfiles",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_resources", [1, 2])
async def test_cloudlab_ambiguous_create_never_adopts_matching_names(matching_resources):
    requests = []
    created = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        if request.method == "POST":
            created.extend(
                {"id": str(uuid4()), "name": "same-name", "project": "research"}
                for _ in range(matching_resources)
            )
            return httpx.Response(
                500,
                json={"error": "The experiment may have been created"},
                headers={"X-Request-ID": EXPERIMENT},
            )
        return httpx.Response(200, json={"experiments": created})

    request = AcquireRequest(
        mode="provision",
        config={**_provision("same-name"), "node_types": ["r6615", "c6620"]},
    )
    operation = intent(request)
    receipt = await provider(respond).acquire(request, operation)

    assert len(created) == matching_resources
    assert requests == [("GET", "/v1/profiles/"), ("POST", "/v1/experiments/")]
    assert receipt.outcome == "unknown"
    assert receipt.resource is None
    assert receipt.diagnostics["provider_request_id"] == EXPERIMENT
    assert receipt.diagnostics["operation_id"] == operation.operation_id
    assert receipt.diagnostics["allocation_id"] == operation.allocation_id
    assert receipt.diagnostics["requested_project"] == "research"
    assert receipt.diagnostics["requested_profile"] == "one-node"


@pytest.mark.asyncio
async def test_cloudlab_invalid_experiment_json_retains_safe_response_diagnostics():
    adapter = provider(
        lambda request: httpx.Response(
            200,
            text=TOKEN,
            headers={"X-Request-ID": EXPERIMENT},
        )
    )

    with pytest.raises(ProviderError) as error:
        await adapter.inspect(ResourceRef(external_id=EXPERIMENT, config=CONFIG))

    assert error.value.code == "invalid_response"
    assert error.value.diagnostics == {
        "provider_operation": "experiments.read",
        "http_status": 200,
        "provider_reason_code": "invalid_json_response",
        "provider_request_id": EXPERIMENT,
        "external_id": EXPERIMENT,
        "requested_project": "research",
    }
    assert TOKEN not in str(error.value) + repr(error.value.diagnostics)


@pytest.mark.asyncio
async def test_cloudlab_rate_limit_diagnostic_keeps_existing_retry_classification():
    adapter = provider(
        lambda request: httpx.Response(429, json={"error": "Request rate limit reached"})
    )
    with pytest.raises(ProviderError) as error:
        await adapter.inspect(ResourceRef(external_id=EXPERIMENT, config=CONFIG))

    assert error.value.code == "rate_limited"
    assert error.value.retryable is True
    assert error.value.diagnostics["http_status"] == 429
    assert error.value.diagnostics["provider_reason_code"] == "rate_limited"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {}, {"message": TOKEN}])
async def test_cloudlab_missing_or_incomplete_400_body_stays_safe(body):
    adapter = provider(
        lambda request: httpx.Response(400, json=body) if body is not None else httpx.Response(400)
    )
    with pytest.raises(ProviderError) as error:
        await adapter.acquire(
            AcquireRequest(
                mode="provision",
                config={
                    "credential_ref": "account-1",
                    "project": "research",
                    "profile_name": "one-node",
                    "profile_project": "PortalProfiles",
                },
            ),
            intent(),
        )

    assert error.value.code == "request_rejected"
    assert error.value.retryable is False
    assert error.value.diagnostics["http_status"] == 400
    assert error.value.diagnostics["provider_reason_code"] == "invalid_request"
    assert TOKEN not in str(error.value) + repr(error.value.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code,retryable,reason",
    [
        (
            httpx.ConnectTimeout("TLS handshake"),
            "temporarily_unavailable",
            True,
            "connect_failed",
        ),
        (
            httpx.ConnectError("certificate verify failed: certificate has expired"),
            "temporarily_unavailable",
            True,
            "tls_verification_failed",
        ),
        (
            httpx.ReadTimeout("create response"),
            "result_unknown",
            False,
            "request_outcome_unknown",
        ),
        (
            httpx.Response(503, json={"error": "server maintenance"}),
            "result_unknown",
            False,
            "provider_server_error",
        ),
    ],
)
async def test_cloudlab_creation_diagnostics_preserve_existing_uncertain_outcome(
    failure, code, retryable, reason
):
    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        if request.method == "GET" and request.url.path.endswith("/experiments/"):
            return httpx.Response(200, json={"experiments": []})
        if isinstance(failure, httpx.RequestError):
            raise failure
        return failure

    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "one-node",
            "profile_project": "PortalProfiles",
        },
    )
    adapter = provider(respond)
    if code == "result_unknown":
        receipt = await adapter.acquire(request, intent(request))
        assert receipt.outcome == "unknown"
        diagnostics = receipt.diagnostics
        assert diagnostics["provider_operation"] == "experiments.create"
        assert diagnostics["provider_reason_code"] == reason
        if isinstance(failure, httpx.RequestError):
            assert "http_status" not in diagnostics
        else:
            assert diagnostics["http_status"] == 503
    else:
        with pytest.raises(ProviderError) as error:
            await adapter.acquire(request, intent(request))
        assert error.value.code == code
        assert error.value.retryable is retryable
        assert error.value.diagnostics["provider_operation"] == "experiments.create"
        assert error.value.diagnostics["provider_reason_code"] == reason
        if isinstance(failure, httpx.Response):
            assert error.value.diagnostics["http_status"] == failure.status_code
        else:
            assert "http_status" not in error.value.diagnostics


@pytest.mark.asyncio
async def test_partial_extension_is_not_success_and_retries_keep_absolute_target():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=resource())

    adapter = provider(respond)
    ref = ResourceRef(external_id=EXPERIMENT, config=CONFIG)
    extension = ExtendRequest(
        target_expires_at=NOW + timedelta(hours=12), reason="research experiments"
    )
    first = await adapter.extend(ref, extension)
    second = await adapter.extend(ref, extension)
    assert first.outcome == second.outcome == "pending"
    puts = [request for request in requests if request.method == "PUT"]
    assert (
        json.loads(puts[0].content)
        == json.loads(puts[1].content)
        == {"expires_at": extension.target_expires_at.isoformat(), "reason": extension.reason}
    )
    assert first.observation.expires_at == NOW + timedelta(hours=4)


@pytest.mark.asyncio
async def test_an_expiration_the_portal_keeps_to_the_minute_confirms_the_extension():
    target = NOW + timedelta(hours=12, seconds=42, microseconds=5)
    stored = target.replace(second=0, microsecond=0)

    def respond(request):
        return httpx.Response(200, json=resource(expires_at=stored.isoformat()))

    receipt = await provider(respond).extend(
        ResourceRef(external_id=EXPERIMENT, config=CONFIG),
        ExtendRequest(target_expires_at=target, reason="research experiments"),
    )
    assert receipt.outcome == "confirmed"
    short = ExtendRequest(target_expires_at=target + timedelta(minutes=1), reason="research")
    assert not short.granted_by(stored)


@pytest.mark.asyncio
async def test_release_checkpoints_documented_204_deletion_without_another_get():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            204 if request.method == "DELETE" else 403,
            text="" if request.method == "DELETE" else TOKEN,
        )

    receipt = await provider(respond).release(
        ResourceRef(external_id=EXPERIMENT, config=CONFIG), intent()
    )
    assert receipt.outcome == "confirmed"
    assert receipt.resource.external_id == EXPERIMENT
    assert receipt.observation.state == "terminated"
    assert receipt.observation.raw_state == "delete_acknowledged"
    assert receipt.observation.scope_complete
    assert [request.method for request in requests] == ["DELETE"]
    assert requests[0].url.path == f"/v1/experiments/{EXPERIMENT}"


@pytest.mark.asyncio
async def test_404_with_valid_other_account_list_never_proves_deletion():
    adapter = provider(
        lambda request: (
            httpx.Response(200, json={"experiments": []})
            if request.url.path.endswith("/experiments/")
            else httpx.Response(404, json={"error": "missing"})
        )
    )
    receipt = await adapter.release(ResourceRef(external_id=EXPERIMENT, config=CONFIG), intent())
    assert receipt.outcome == "unknown"
    assert receipt.observation is None
    ref = ResourceRef(external_id=EXPERIMENT, config=CONFIG)
    # A list endpoint can authenticate another valid account while the
    # original resource remains alive and inaccessible to that account.
    await adapter.validate_connection(CONFIG["credential_ref"])
    with pytest.raises(ProviderError) as error:
        await adapter.inspect(ref)
    assert error.value.code == "resource_visibility_unknown"


@pytest.mark.asyncio
async def test_project_mismatch_is_never_adopted_even_for_terminal_status():
    adapter = provider(
        lambda request: httpx.Response(
            200, json=resource(project="other-project", status="terminated")
        )
    )
    with pytest.raises(ProviderError) as error:
        await adapter.inspect(ResourceRef(external_id=EXPERIMENT, config=CONFIG))
    assert error.value.code == "resource_identity_mismatch"


@pytest.mark.asyncio
async def test_lost_delete_acknowledgement_requires_explicit_terminal_evidence():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "DELETE":
            raise httpx.ReadTimeout("acknowledgement lost", request=request)
        return httpx.Response(404, json={"error": "missing"})

    adapter = provider(respond)
    ref = ResourceRef(external_id=EXPERIMENT, config=CONFIG)
    receipt = await adapter.release(ref, intent())
    assert receipt.outcome == "unknown" and receipt.observation is None
    with pytest.raises(ProviderError) as error:
        await adapter.inspect(ref)
    assert error.value.code == "resource_visibility_unknown"
    assert [request.method for request in requests] == ["DELETE", "GET"]
    adapter = provider(lambda request: httpx.Response(200, json=resource(status="terminated")))
    observation = await adapter.inspect(ref)
    assert observation.state == "terminated" and observation.ref == ref


@pytest.mark.asyncio
async def test_release_rejects_forbidden_and_undocumented_success_responses():
    ref = ResourceRef(external_id=EXPERIMENT, config=CONFIG)
    adapter = provider(lambda request: httpx.Response(403, json={"error": TOKEN}))
    with pytest.raises(ProviderError) as error:
        await adapter.release(ref, intent())
    assert error.value.code == "permission_denied" and TOKEN not in str(error.value)
    adapter = provider(lambda request: httpx.Response(200, json={"error": TOKEN}))
    receipt = await adapter.release(ref, intent())
    assert receipt.outcome == "unknown" and receipt.observation is None
    assert TOKEN not in receipt.model_dump_json()


def _ssh_process(output: bytes, returncode: int = 0) -> SimpleNamespace:
    """A finished ssh client whose stdout already holds ``output``."""
    stdout = asyncio.StreamReader()
    stdout.feed_data(output)
    stdout.feed_eof()
    return SimpleNamespace(
        stdin=MagicMock(drain=AsyncMock()),
        stdout=stdout,
        returncode=returncode,
        wait=AsyncMock(return_value=returncode),
        kill=MagicMock(),
    )


@pytest.mark.asyncio
async def test_openssh_uses_pinned_host_keys_and_does_not_place_key_in_arguments(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.side_effect = ["PRIVATE KEY\n", "node.example.org ssh-ed25519 VERIFIED\n"]
    process = _ssh_process(b"identity")
    captured = {}

    async def launch(*args, **kwargs):
        from pathlib import Path

        captured["args"] = args
        captured["key_path"] = args[args.index("-i") + 1]
        assert Path(captured["key_path"]).read_text() == "PRIVATE KEY\n"
        assert Path(captured["key_path"]).stat().st_mode & 0o777 == 0o600
        assert "SSH_AUTH_SOCK" not in kwargs["env"]
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", launch)
    text = await OpenSshTransport(resolver).run(
        SshConnection.model_validate(SSH), "cat /etc/machine-id"
    )
    assert text == "identity"
    args = captured["args"]
    assert "StrictHostKeyChecking=yes" in args and "IdentityAgent=none" in args
    assert "PRIVATE KEY" not in " ".join(args)
    from pathlib import Path

    assert not Path(captured["key_path"]).exists()


@pytest.mark.asyncio
async def test_openssh_explicitly_unchecked_mode_uses_no_host_key_files(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "PRIVATE KEY\n"
    process = _ssh_process(b"identity")
    captured = {}

    async def launch(*args, **_kwargs):
        captured["args"] = args
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", launch)
    connection = SshConnection(
        address="node.example.org",
        username="agent",
        credential_ref="ssh-1",
        known_hosts_ref=UNCHECKED_CLOUDLAB_HOST_KEY_REF,
    )
    assert await OpenSshTransport(resolver).run(connection, "cat /etc/machine-id") == "identity"
    resolver.resolve.assert_awaited_once_with("ssh-1")
    args = captured["args"]
    assert "StrictHostKeyChecking=no" in args
    assert "UserKnownHostsFile=/dev/null" in args
    assert "GlobalKnownHostsFile=/dev/null" in args
    assert not any(str(argument).startswith("UserKnownHostsFile=/tmp/") for argument in args)


def test_unchecked_marker_does_not_override_a_registered_host_key():
    connection = SshConnection(
        address="node.example.org",
        username="agent",
        credential_ref="ssh-1",
        known_hosts_ref=UNCHECKED_CLOUDLAB_HOST_KEY_REF,
        host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG9vcHNpZGFpc3k=",
    )
    assert connection.verify_host_key


@pytest.mark.asyncio
async def test_openssh_strict_mode_refuses_missing_host_material_before_launch(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "PRIVATE KEY\n"
    launch = AsyncMock()
    monkeypatch.setattr("asyncio.create_subprocess_exec", launch)
    connection = SshConnection(address="node.example.org", username="agent", credential_ref="ssh-1")

    with pytest.raises(ProviderError) as refused:
        await OpenSshTransport(resolver).run(connection, "cat /etc/machine-id")

    assert refused.value.code == "ssh_configuration"
    launch.assert_not_awaited()


def _unchecked_connection() -> SshConnection:
    return SshConnection(
        address="node.example.org",
        username="agent",
        credential_ref="ssh-1",
        known_hosts_ref=UNCHECKED_CLOUDLAB_HOST_KEY_REF,
    )


def _remote(monkeypatch, program: str) -> list[dict]:
    """Run ``program`` in place of ssh, with the pipes the transport asked for."""
    real = asyncio.create_subprocess_exec
    launched: list[dict] = []

    async def launch(*_args, **kwargs):
        launched.append(kwargs)
        return await real(sys.executable, "-c", program, **kwargs)

    monkeypatch.setattr("asyncio.create_subprocess_exec", launch)
    return launched


@pytest.mark.asyncio
async def test_openssh_refuses_a_remote_that_keeps_writing_without_waiting_for_eof(monkeypatch):
    """The cap acts while the bytes arrive, not after the remote decides to stop."""
    resolver = AsyncMock()
    resolver.resolve.return_value = "PRIVATE KEY\n"
    # A node whose machine-id became an endless stream: past the cap, then the
    # connection stays open. Reading to EOF would wait out the whole timeout.
    launched = _remote(
        monkeypatch,
        "import sys, time\n"
        "sys.stdout.buffer.write(b'0' * (5 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
    )
    started = time.monotonic()
    with pytest.raises(ProviderError) as refused:
        await OpenSshTransport(resolver).run(
            _unchecked_connection(), "cat /etc/machine-id", timeout=20
        )
    assert refused.value.code == "invalid_response"
    assert time.monotonic() - started < 15
    # Remote diagnostics are never read, so they cannot be made to pile up.
    assert launched[0]["stderr"] == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_openssh_delivers_a_large_stdin_while_reading_the_answer(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.return_value = "PRIVATE KEY\n"
    _remote(
        monkeypatch,
        "import sys\nsys.stdout.write(str(len(sys.stdin.buffer.read())))\n",
    )
    archive = b"x" * (8 * 1024 * 1024)
    answer = await OpenSshTransport(resolver).run(
        _unchecked_connection(), "deploy", stdin=archive, timeout=20
    )
    assert answer == str(len(archive))


@pytest.mark.asyncio
async def test_cloudlab_stops_reading_an_oversized_body_at_the_limit():
    pulled = 0

    async def endless():
        nonlocal pulled
        for _ in range(256):
            pulled += 1
            yield b" " * (64 * 1024)

    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=endless())

    with pytest.raises(ProviderError) as refused:
        await provider(respond).validate_connection("account-1")
    assert refused.value.code == "invalid_response"
    # 4 MiB is 64 such chunks; the next one is refused instead of buffered.
    assert pulled == 65


@pytest.mark.asyncio
async def test_cloudlab_drops_an_oversized_error_body_and_keeps_its_status():
    pulled = 0

    async def endless():
        nonlocal pulled
        for _ in range(256):
            pulled += 1
            yield b" " * (4 * 1024)

    def respond(request):
        if request.method == "GET":
            return _profile_list()
        return httpx.Response(429, content=endless())

    with pytest.raises(ProviderError) as refused:
        await provider(respond).acquire(
            AcquireRequest(mode="provision", config=_provision()), intent()
        )
    # The capacity diagnostic is unreadable, so the 429 stays a rate limit.
    assert (refused.value.code, refused.value.retryable) == ("rate_limited", True)
    # 16 KiB is 4 such chunks; reading stops at the next one.
    assert pulled == 5


@pytest.mark.asyncio
async def test_cloudlab_refuses_a_compressed_body_it_did_not_ask_for():
    def respond(request):
        return httpx.Response(
            200, content=b"\x1f\x8b compressed", headers={"Content-Encoding": "gzip"}
        )

    with pytest.raises(ProviderError) as refused:
        await provider(respond).validate_connection("account-1")
    assert refused.value.code == "temporarily_unavailable"
    assert TOKEN not in str(refused.value)


@pytest.mark.asyncio
async def test_successful_extend_followed_by_read_failure_is_unknown_not_retryable():
    def respond(request):
        return (
            httpx.Response(200, json=resource()) if request.method == "PUT" else httpx.Response(503)
        )

    receipt = await provider(respond).extend(
        ResourceRef(external_id=EXPERIMENT, config=CONFIG),
        ExtendRequest(target_expires_at=NOW + timedelta(hours=8), reason="research"),
    )
    assert receipt.outcome == "unknown" and receipt.resource.external_id == EXPERIMENT
    assert receipt.observation is None


@pytest.mark.asyncio
async def test_wrong_endpoint_404_is_not_terminal_resource_evidence():
    adapter = provider(lambda request: httpx.Response(404, text="default proxy page"))
    with pytest.raises(ProviderError):
        await adapter.inspect(ResourceRef(external_id=EXPERIMENT, config=CONFIG))


@pytest.mark.asyncio
async def test_duplicate_provider_nodes_never_prove_complete_scope():
    duplicate = resource()
    duplicate["aggregates"]["urn:aggregate"]["nodes"] *= 2
    adapter = provider(lambda request: httpx.Response(200, json=duplicate))
    with pytest.raises(ProviderError, match="duplicate node"):
        await adapter.inspect(ResourceRef(external_id=EXPERIMENT, config=CONFIG))


@pytest.mark.asyncio
async def test_http_200_error_body_does_not_acknowledge_extension_or_leak_diagnostic():
    adapter = provider(lambda request: httpx.Response(200, json={"error": TOKEN, "code": 403}))
    receipt = await adapter.extend(
        ResourceRef(external_id=EXPERIMENT, config=CONFIG),
        ExtendRequest(target_expires_at=NOW + timedelta(hours=8), reason="research"),
    )
    assert receipt.outcome == "unknown" and receipt.observation is None
    assert TOKEN not in receipt.model_dump_json()
    with pytest.raises(ProviderError):
        await adapter.validate_connection("account-1")


@pytest.mark.asyncio
async def test_cloudlab_sends_boolean_bindings_in_the_portal_string_form():
    requests = []

    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        requests.append(request)
        return httpx.Response(201, json={"id": EXPERIMENT})

    config = {
        "credential_ref": "account-1",
        "project": "research",
        "profile_name": "small-lan",
        "profile_project": "PortalProfiles",
        "bindings": {
            "phystype": "r6615",
            "tempFileSystemMax": True,
            "useVMs": False,
            "nodeCount": 1,
        },
    }
    await provider(respond).acquire(AcquireRequest(mode="provision", config=config), intent())
    assert json.loads(requests[0].content)["bindings"] == {
        "phystype": "r6615",
        "tempFileSystemMax": "True",
        "useVMs": "False",
        "nodeCount": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("certificate has expired"), httpx.ConnectTimeout("handshake timed out")],
)
async def test_a_request_that_never_left_is_retryable_even_when_it_mutates(failure):
    def respond(request):
        raise failure

    adapter = provider(respond)
    request = AcquireRequest(
        mode="provision",
        config={
            "credential_ref": "account-1",
            "project": "research",
            "profile_name": "small-lan",
            "profile_project": "PortalProfiles",
        },
    )
    with pytest.raises(ProviderError) as created:
        await adapter.acquire(request, intent())
    assert (created.value.code, created.value.retryable) == ("temporarily_unavailable", True)
    with pytest.raises(ProviderError) as extended:
        await adapter.extend(
            ResourceRef(external_id=EXPERIMENT, config=CONFIG),
            ExtendRequest(target_expires_at=NOW + timedelta(days=1), reason="research"),
        )
    assert (extended.value.code, extended.value.retryable) == ("temporarily_unavailable", True)
    assert TOKEN not in str(created.value) + str(extended.value)


REGISTERING = {
    "credential_ref": "account-1",
    "project": "research",
    "profile_name": "cloud-agent-node",
    "profile_project": "research",
    "bindings": {"phystype": "r6615"},
    "ssh_username": "agent",
    "ssh_credential_ref": "ssh-1",
    "ssh_known_hosts_ref": "ssh-1:known_hosts",
    "host_key_source": "registration",
}


@pytest.mark.asyncio
async def test_the_registration_token_rides_only_on_the_create_request():
    requests = []

    def respond(request):
        if request.method == "GET" and request.url.path.endswith("/profiles/"):
            return _profile_list()
        requests.append(request)
        return httpx.Response(201, json={"id": EXPERIMENT})

    secret = "r" * 43
    request = AcquireRequest(
        mode="provision",
        config=REGISTERING,
        registration=NodeRegistration(url="https://example.org/register", token=secret),
    )
    receipt = await provider(respond).acquire(request, intent())
    bindings = json.loads(requests[0].content)["bindings"]
    assert bindings["registrationUrl"] == "https://example.org/register"
    assert bindings["registrationToken"] == secret
    # What the control plane persists never holds the token.
    assert secret not in receipt.model_dump_json()
    assert secret not in repr(request)


@pytest.mark.asyncio
async def test_registration_survives_capacity_fallback_without_persisting_its_token():
    payloads = []

    def respond(request):
        if request.method == "GET":
            return _profile_list()
        payloads.append(json.loads(request.content))
        return (
            httpx.Response(429, json={"error": "Insufficient physical nodes"})
            if len(payloads) == 1
            else httpx.Response(201, json={"id": EXPERIMENT})
        )

    secret = "r" * 43
    config = {**REGISTERING, "bindings": {}, "node_types": ["r6615", "c6620"]}
    request = AcquireRequest(
        mode="provision",
        config=config,
        registration=NodeRegistration(url="https://example.org/register", token=secret),
    )
    receipt = await provider(respond).acquire(request, intent())
    assert [payload["bindings"]["phystype"] for payload in payloads] == ["r6615", "c6620"]
    assert all(payload["bindings"]["registrationToken"] == secret for payload in payloads)
    assert all(
        payload["bindings"]["registrationUrl"] == "https://example.org/register"
        for payload in payloads
    )
    assert receipt.resource.config["bindings"] == {"phystype": "c6620"}
    assert secret not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_a_registering_node_is_never_created_without_its_token():
    adapter = provider(lambda request: httpx.Response(201, json={"id": EXPERIMENT}))
    with pytest.raises(ProviderError) as refused:
        await adapter.acquire(AcquireRequest(mode="provision", config=REGISTERING), intent())
    assert refused.value.code == "registration_unavailable"


@pytest.mark.asyncio
async def test_a_registering_node_is_reached_through_its_registered_key_only():
    def respond(request):
        if request.url.path.endswith("/manifests"):
            return httpx.Response(
                200,
                json={
                    "urn:aggregate": '<rspec xmlns="http://www.geni.net/resources/rspec/3"><node client_id="vm1"><services><login authentication="ssh-keys" username="agent" hostname="node.example.org" port="22"/></services></node></rspec>'
                },
            )
        return httpx.Response(200, json=resource())

    config = {**REGISTERING, "external_id": EXPERIMENT}
    observation = await provider(respond).inspect(
        ResourceRef(external_id=EXPERIMENT, config=config)
    )
    connection = observation.nodes[0].connection
    assert connection.known_hosts_ref is None and connection.host_key is None
    assert connection.verify_host_key is True


def test_registration_applies_only_to_nodes_this_control_plane_creates():
    adapter = provider(lambda request: httpx.Response(200, json=resource()))
    without_list = {
        key: value for key, value in REGISTERING.items() if key != "ssh_known_hosts_ref"
    }
    assert adapter.validate_config("provision", without_list)["host_key_source"] == "registration"
    with pytest.raises(ProviderError):
        adapter.validate_config("attach", {**without_list, "external_id": EXPERIMENT})
    with pytest.raises(ProviderError):
        adapter.validate_config("provision", {**without_list, "host_key_source": "known_hosts"})
