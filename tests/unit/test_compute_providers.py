"""Compute adapters exercise actual wire contracts without real accounts/resources."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from cloud_agent.ports.external.compute_provider import (
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
    assert receipt.observation.nodes[0].connection.port == 2207
    assert (
        receipt.observation.nodes[0].instance_identity
        == EXPERIMENT + ":urn:aggregate:vm1:urn:node:1"
    )
    assert all(request.method == "GET" for request in requests)
    assert TOKEN not in receipt.model_dump_json()


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


@pytest.mark.asyncio
async def test_cloudlab_provision_retains_returned_uuid_and_matches_create_contract():
    requests = []

    def respond(request):
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
    assert requests[0].url.path == "/v1/experiments/"


@pytest.mark.asyncio
async def test_create_timeout_recovery_never_reposts_or_adopts_a_name_collision():
    requests = []

    def respond(request):
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
        (503, "temporarily_unavailable"),
        (429, "capacity_unavailable"),
        (302, "request_rejected"),
    ],
)
async def test_provider_error_diagnostics_never_expose_secrets_or_call_redirect(status, code):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text=TOKEN, headers={"Location": "https://attacker.example/"})

    with pytest.raises(ProviderError) as error:
        await provider(respond).inspect(ResourceRef(external_id=EXPERIMENT, config=CONFIG))
    assert error.value.code == code
    assert TOKEN not in str(error.value)
    assert len(requests) == 1


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


@pytest.mark.asyncio
async def test_openssh_uses_pinned_host_keys_and_does_not_place_key_in_arguments(monkeypatch):
    resolver = AsyncMock()
    resolver.resolve.side_effect = ["PRIVATE KEY\n", "node.example.org ssh-ed25519 VERIFIED\n"]
    process = AsyncMock(returncode=0)
    process.communicate.return_value = (b"identity", b"")
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
