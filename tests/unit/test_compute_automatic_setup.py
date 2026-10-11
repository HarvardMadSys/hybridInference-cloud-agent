"""Automatic setup checkpoints credentials and admits only fully provisioned routes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from cloud_agent import settings
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.application.compute_setup import AutomaticComputeSetup
from cloud_agent.compute_config import ComputeConfiguration, ComputeSecretResolver
from cloud_agent.compute_registry import ComputeProviderRegistry
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.ports.external.compute_provider import ProviderError, SshConnection
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.providers.compute.setup_tunnels import ControlPlaneEndpoint, generate_tunnel_key
from cloud_agent.schemas_compute import AutomaticDeploymentSetup

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

SHA = "a" * 40
BROKER_TOKEN = "private-existing-broker-credential"
ENDPOINT = ControlPlaneEndpoint(
    main_port=8000,
    mcp_port=8001,
    gateway_ip="172.30.0.1",
    machine_identity="b" * 64,
)


class MemoryRepository:
    """Persist committed profile checkpoints across application restarts."""

    def __init__(self) -> None:
        self.connections: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.defaults: dict[str, dict[str, Any]] = {}
        self.yield_on_insert = False
        self.activation_calls = 0

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        return deepcopy(self.connections.get(connection_id))

    async def provider_defaults(self, provider_id: str) -> dict[str, Any]:
        return deepcopy(self.defaults.get(provider_id, {}))

    async def set_provider_defaults(self, provider_id: str, defaults: dict[str, Any]) -> None:
        self.defaults[provider_id] = deepcopy(defaults)

    async def list_deployment_profiles(self) -> list[dict[str, Any]]:
        return [
            deepcopy(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"source_bundle", "secret_ciphertext"}
                }
            )
            for row in self.profiles.values()
        ]

    async def get_deployment_profile(self, profile_id: str) -> dict[str, Any] | None:
        return deepcopy(self.profiles.get(profile_id))

    async def save_deployment_profile(self, *, profile_id: str, **inputs: Any) -> dict[str, Any]:
        if self.yield_on_insert:
            await asyncio.sleep(0)
        if profile_id in self.profiles:
            raise ValueError("deployment_profile_exists")
        metadata = inputs["configuration"].get("automatic_setup", {})
        if metadata and any(
            row["configuration"].get("automatic_setup", {}).get("request_id")
            == metadata["request_id"]
            and row["configuration"]["automatic_setup"].get("slot") == metadata["slot"]
            for row in self.profiles.values()
        ):
            raise ValueError("deployment_profile_exists")
        self.profiles[profile_id] = deepcopy({"id": profile_id, **inputs})
        return deepcopy(self.profiles[profile_id])

    async def activate_automatic_profiles(
        self, *, profile_ids: list[str], request_id: str, request_fingerprint: str
    ) -> None:
        rows = [self.profiles[profile_id] for profile_id in profile_ids]
        for row in rows:
            metadata = row["configuration"]["automatic_setup"]
            if (
                metadata["request_id"] != request_id
                or metadata["fingerprint"] != request_fingerprint
            ):
                raise ValueError("idempotency_conflict")
        self.activation_calls += 1
        for row in rows:
            row["configuration"]["automatic_setup"]["ready"] = True


class FakeTunnels:
    """Represent external idempotent authorizations and a recoverable partial failure."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository
        self.endpoint = ENDPOINT
        self.gateway_identity = "c" * 64
        self.fail_control_once = False
        self.calls: list[tuple[Any, ...]] = []
        self.authorizations: dict[tuple[str, str], str] = {}

    async def discover_control_plane(
        self, connection: SshConnection, deployment_sha: str
    ) -> ControlPlaneEndpoint:
        self.calls.append(("discover", connection.address, deployment_sha))
        await asyncio.sleep(0)
        return self.endpoint

    async def check_gateway(self, connection: SshConnection, port: int) -> str:
        self.calls.append(("check", connection.address, port))
        await asyncio.sleep(0)
        return self.gateway_identity

    def authorize(self, profile_id: str, role: str, key: str) -> None:
        # Remote mutations may begin only after every slot has a durable checkpoint.
        metadata = self.repository.profiles[profile_id]["configuration"]["automatic_setup"]
        assert (
            len(
                [
                    row
                    for row in self.repository.profiles.values()
                    if row["configuration"].get("automatic_setup", {}).get("request_id")
                    == metadata["request_id"]
                ]
            )
            == metadata["slots"]
        )
        previous = self.authorizations.setdefault((profile_id, role), key)
        assert previous == key, "a retry attempted to replace a provisioned tunnel key"

    async def provision_gateway(
        self, connection: SshConnection, public_key: str, port: int, profile_id: str
    ) -> None:
        self.calls.append(("gateway", connection.address, public_key, port, profile_id))
        self.authorize(profile_id, "gateway", public_key)
        await asyncio.sleep(0)

    async def provision_control(
        self,
        connection: SshConnection,
        mcp_key: str,
        control_key: str,
        endpoint: ControlPlaneEndpoint,
        reverse_port: int,
        profile_id: str,
    ) -> None:
        self.calls.append(
            ("control", connection.address, mcp_key, control_key, reverse_port, profile_id)
        )
        if self.fail_control_once:
            self.fail_control_once = False
            raise ProviderError("setup_failed", "Stable machine temporarily unavailable")
        self.authorize(profile_id, "mcp", mcp_key)
        self.authorize(profile_id, "control", control_key)
        await asyncio.sleep(0)


class MemoryResolver(ComputeSecretResolver):
    """Use in-memory mounted configuration with the real encrypted credential store."""

    def __init__(
        self,
        secrets: ComputeSecretStore,
        configuration: ComputeConfiguration,
        mounted: dict[str, str],
    ) -> None:
        super().__init__(secrets, configuration)
        self.mounted = mounted

    async def resolve(self, ref: str) -> str:
        if ref in self.mounted:
            return self.mounted[ref]
        return await super().resolve(ref)


@dataclass
class Harness:
    repository: MemoryRepository
    secrets: ComputeSecretStore
    resolver: ComputeSecretResolver
    configuration: ComputeConfiguration
    tunnels: FakeTunnels
    release_directory: Path

    def restart(self) -> AutomaticComputeSetup:
        return AutomaticComputeSetup(
            repository=self.repository,
            secrets=self.secrets,
            resolver=self.resolver,
            configuration=self.configuration,
            tunnels=self.tunnels,
            validate_bundle=ComputeCommands._validate_bundle,
            release_directory=self.release_directory,
        )

    def commands(self) -> ComputeCommands:
        return ComputeCommands(
            repository=self.repository,
            registry=ComputeProviderRegistry([]),
            secrets=self.secrets,
            configuration=self.configuration,
            automatic_setup=self.restart(),
        )

    def assets(self, profile_id: str) -> dict[str, Any]:
        row = self.repository.profiles[profile_id]
        return json.loads(
            self.secrets.decrypt(
                "profile:" + profile_id, "deployment_profile", row["secret_ciphertext"]
            )
        )


def request(**updates: Any) -> AutomaticDeploymentSetup:
    return AutomaticDeploymentSetup.model_validate(
        {
            "request_id": "prepare-one",
            "gateway": {
                "connection_id": "gateway-key",
                "address": "gateway.example.org",
                "username": "operator",
            },
            "control": {
                "connection_id": "control-key",
                "address": "control.example.org",
                "username": "operator",
            },
            "chat_network_pool": "172.29.0.0/16",
            "dns_server": "1.1.1.1",
            "protected_cidrs": ["192.0.2.0/24"],
            "slots": 2,
            **updates,
        }
    )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    settings.reset_settings()
    monkeypatch.setenv("AGENT_SETTINGS_ENCRYPTION_KEY", "s" * 32)
    settings.apply_settings(1, settings.encode_changes({"workspace_broker_token": BROKER_TOKEN}))
    monkeypatch.setenv("AGENT_DEPLOYMENT_SHA", SHA)
    directory = tmp_path / "release"
    directory.mkdir()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in (
            "deploy/host/sandbox_host.sh",
            "deploy/host/setup_kata_runtime.sh",
            "deploy/host/docker-compose.sandbox.yml",
            "pyproject.toml",
        ):
            info = tarfile.TarInfo(name)
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    bundle = output.getvalue()
    (directory / "source.tar.gz").write_bytes(bundle)
    (directory / "release.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "source_commit": SHA,
                "source_sha256": hashlib.sha256(bundle).hexdigest(),
                "bundle": "source.tar.gz",
            }
        )
    )
    repository = MemoryRepository()
    secrets = ComputeSecretStore(repository, key="k" * 32)
    configuration = ComputeConfiguration()
    resolver = ComputeSecretResolver(secrets, configuration)
    for connection_id, address in (
        ("gateway-key", "gateway.example.org"),
        ("control-key", "control.example.org"),
    ):
        repository.connections[connection_id] = {
            "id": connection_id,
            "provider_id": "existing_ssh",
            "status": "verified",
            "configuration": {"known_hosts": address + " " + generate_tunnel_key().public_key},
            "secret_ciphertext": secrets.encrypt(
                connection_id, "existing_ssh", "private-management-key"
            ),
        }
    yield Harness(repository, secrets, resolver, configuration, FakeTunnels(repository), directory)
    settings.reset_settings()


@pytest.mark.asyncio
async def test_failed_provisioning_stays_unavailable_and_restart_reuses_persisted_keys(
    harness: Harness,
) -> None:
    harness.tunnels.fail_control_once = True
    with pytest.raises(ProviderError, match="temporarily unavailable"):
        await harness.restart().create(request())
    assert len(harness.repository.profiles) == 2
    ciphertexts = {
        key: row["secret_ciphertext"] for key, row in harness.repository.profiles.items()
    }
    assets = {key: harness.assets(key) for key in ciphertexts}
    assert harness.repository.activation_calls == 0
    assert await harness.commands().deployment_profiles() == {}
    assert (await harness.commands().catalog())["deployment_profiles"] == []
    interrupted_status = await harness.commands().automatic_setup_status()
    assert interrupted_status["pending_request_id"] == "prepare-one"
    assert interrupted_status["defaults"] == request().model_dump(
        mode="json", exclude={"request_id"}
    )

    result = await harness.restart().create(request())
    assert {profile["id"] for profile in result["profiles"]} == set(ciphertexts)
    assert {
        key: row["secret_ciphertext"] for key, row in harness.repository.profiles.items()
    } == ciphertexts
    assert {key: harness.assets(key) for key in ciphertexts} == assets
    assert set(await harness.commands().deployment_profiles()) == set(ciphertexts)
    calls = list(harness.tunnels.calls)
    assert await harness.restart().create(request()) == result
    assert harness.tunnels.calls == calls
    assert (await harness.commands().automatic_setup_status())["pending_request_id"] is None


@pytest.mark.asyncio
async def test_responses_expose_only_profile_names_and_credentials_are_encrypted(
    harness: Harness,
) -> None:
    result = await harness.commands().create_automatic_setup(request())
    responses = json.dumps(
        [
            result,
            await harness.commands().catalog(),
            await harness.commands().automatic_setup_status(),
        ]
    )
    assert all(set(profile) == {"id", "label"} for profile in result["profiles"])
    assert BROKER_TOKEN not in responses
    assert "private-management-key" not in responses
    assert "OPENSSH PRIVATE KEY" not in responses
    for profile in result["profiles"]:
        assets = harness.assets(profile["id"])
        assert len(set(assets["public_keys"].values())) == 3
        assert len(assets["files"]) == 6
        for value in assets["files"].values():
            assert value not in responses
        row = harness.repository.profiles[profile["id"]]
        assert BROKER_TOKEN not in json.dumps(
            {key: value for key, value in row.items() if key != "source_bundle"}
        )


@pytest.mark.asyncio
async def test_request_id_cannot_be_reused_for_different_configuration(harness: Harness) -> None:
    await harness.restart().create(request())
    calls = list(harness.tunnels.calls)
    with pytest.raises(ComputeRefusal) as exc:
        await harness.restart().create(request(dns_server="9.9.9.9"))
    assert exc.value.code == "idempotency_conflict"
    assert harness.tunnels.calls == calls
    assert len(harness.repository.profiles) == 2


@pytest.mark.parametrize(
    "defect", ["digest", "revision", "missing", "symlink", "filename", "format"]
)
def test_release_integrity_and_revision_fail_before_provisioning(
    harness: Harness, defect: str
) -> None:
    directory = harness.release_directory
    manifest = json.loads((directory / "release.json").read_text())
    if defect == "digest":
        (directory / "source.tar.gz").write_bytes(b"corrupted")
    elif defect == "revision":
        manifest["source_commit"] = "b" * 40
    elif defect == "missing":
        (directory / "source.tar.gz").unlink()
    elif defect == "symlink":
        source = directory / "source.tar.gz"
        source.rename(directory / "external.tar.gz")
        source.symlink_to(directory / "external.tar.gz")
    elif defect == "filename":
        manifest["bundle"] = "../other.tar.gz"
    else:
        manifest["format_version"] = 2
    (directory / "release.json").write_text(json.dumps(manifest))
    status = harness.restart().status()
    assert status["available"] is False
    assert status["blocked_reasons"][0]["code"] == (
        "automatic_release_mismatch" if defect == "revision" else "automatic_release_unavailable"
    )
    assert harness.tunnels.calls == []
    assert harness.repository.profiles == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["wrong-provider", "unverified", "missing"])
async def test_only_existing_verified_ssh_connections_can_provision(
    harness: Harness, defect: str
) -> None:
    if defect == "wrong-provider":
        harness.repository.connections["control-key"]["provider_id"] = "cloudlab"
    elif defect == "unverified":
        harness.repository.connections["control-key"]["status"] = "unverified"
    else:
        del harness.repository.connections["control-key"]
    with pytest.raises(ComputeRefusal) as exc:
        await harness.restart().create(request())
    assert exc.value.code == "ssh_configuration"
    assert harness.tunnels.calls == []
    assert harness.repository.profiles == {}


@pytest.mark.asyncio
async def test_changed_infrastructure_cannot_rewrite_pending_profile_inputs(
    harness: Harness,
) -> None:
    harness.tunnels.fail_control_once = True
    with pytest.raises(ProviderError):
        await harness.restart().create(request())
    before = deepcopy(harness.repository.profiles)
    harness.tunnels.endpoint = ENDPOINT.model_copy(update={"machine_identity": "c" * 64})
    with pytest.raises(ComputeRefusal) as exc:
        await harness.restart().create(request())
    assert exc.value.code == "automatic_infrastructure_changed"
    assert harness.repository.profiles == before


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["main_port", "machine_identity"])
async def test_concurrent_checkpoint_discovery_rejects_changed_endpoint_before_authorization(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, changed_field: str
) -> None:
    harness.tunnels.fail_control_once = True
    with pytest.raises(ProviderError):
        await harness.restart().create(request())
    before = deepcopy(harness.repository.profiles)
    authorized = dict(harness.tunnels.authorizations)
    list_profiles = harness.repository.list_deployment_profiles
    first_snapshot = True

    async def snapshot_before_concurrent_insert() -> list[dict[str, Any]]:
        nonlocal first_snapshot
        if first_snapshot:
            first_snapshot = False
            return []
        return await list_profiles()

    monkeypatch.setattr(
        harness.repository, "list_deployment_profiles", snapshot_before_concurrent_insert
    )
    harness.tunnels.endpoint = ENDPOINT.model_copy(
        update={changed_field: 8010 if changed_field == "main_port" else "d" * 64}
    )
    calls = len(harness.tunnels.calls)
    with pytest.raises(ComputeRefusal) as exc:
        await harness.restart().create(request())
    assert exc.value.code == "automatic_infrastructure_changed"
    assert harness.repository.profiles == before
    assert harness.tunnels.authorizations == authorized
    assert all(call[0] not in {"gateway", "control"} for call in harness.tunnels.calls[calls:])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("same_machine", "gateway_port", "accepted"),
    [
        (True, ENDPOINT.main_port, False),
        (True, ENDPOINT.mcp_port, False),
        (False, ENDPOINT.main_port, True),
        (True, 8080, True),
    ],
)
async def test_gateway_and_control_plane_need_distinct_listeners_when_co_located(
    harness: Harness, same_machine: bool, gateway_port: int, accepted: bool
) -> None:
    harness.tunnels.gateway_identity = ENDPOINT.machine_identity if same_machine else "c" * 64
    if accepted:
        result = await harness.restart().create(request(slots=1, gateway_port=gateway_port))
        assert len(result["profiles"]) == 1
    else:
        with pytest.raises(ComputeRefusal) as exc:
            await harness.restart().create(request(slots=1, gateway_port=gateway_port))
        assert exc.value.code == "automatic_listener_conflict"
        assert exc.value.status == 422
        assert harness.repository.profiles == {}
        assert harness.repository.defaults == {}
        assert harness.tunnels.authorizations == {}
        assert all(call[0] not in {"gateway", "control"} for call in harness.tunnels.calls)


@pytest.mark.asyncio
async def test_generated_broker_route_skips_both_control_plane_listener_ports(
    harness: Harness,
) -> None:
    first = int(hashlib.sha256(b"prepare-one").hexdigest()[:8], 16) % 30000
    main_port = 20000 + first
    mcp_port = 20000 + (first + 1) % 30000
    harness.tunnels.endpoint = ENDPOINT.model_copy(
        update={"main_port": main_port, "mcp_port": mcp_port}
    )
    result = await harness.restart().create(request(slots=1))
    metadata = harness.repository.profiles[result["profiles"][0]["id"]]["configuration"][
        "automatic_setup"
    ]
    assert metadata["reverse_port"] == 20000 + (first + 2) % 30000


@pytest.mark.asyncio
async def test_machine_aliases_share_route_reservations(harness: Harness) -> None:
    first = await harness.restart().create(request(slots=1))
    other = request(request_id="alias-request", slots=1)
    other.control.address = "control-alias.example.org"
    second = await harness.restart().create(other)
    assert first["profiles"][0]["id"] != second["profiles"][0]["id"]
    ports = {
        row["configuration"]["automatic_setup"]["reverse_port"]
        for row in harness.repository.profiles.values()
    }
    assert len(ports) == 2
    assert {
        row["configuration"]["automatic_setup"]["endpoint"]["machine_identity"]
        for row in harness.repository.profiles.values()
    } == {ENDPOINT.machine_identity}


@pytest.mark.asyncio
async def test_operator_and_custom_routes_are_not_reused(harness: Harness) -> None:
    first_port = 20000 + int(hashlib.sha256(b"prepare-one").hexdigest()[:8], 16) % 30000
    profile = DeploymentProfile(
        id="legacy",
        label="Legacy",
        source_bundle="/unused/source.tar.gz",
        source_sha256="b" * 64,
        deployment_sha=SHA,
        environment_ref="deployment:legacy-host",
    )
    harness.configuration = ComputeConfiguration(
        profiles={profile.id: profile}, deployment_secrets={"legacy-host": "/mounted/legacy.env"}
    )
    harness.resolver = MemoryResolver(
        harness.secrets,
        harness.configuration,
        {
            "deployment:legacy-host": f"AGENT_BROKER_REVERSE_BIND={ENDPOINT.gateway_ip}\nAGENT_BROKER_REVERSE_PORT={first_port}\n"
        },
    )
    custom_assets = {
        "environment": f"AGENT_BROKER_REVERSE_BIND={ENDPOINT.gateway_ip}\nAGENT_BROKER_REVERSE_PORT={first_port + 1}\n",
        "files": {},
    }
    harness.repository.profiles["custom"] = {
        "id": "custom",
        "label": "Custom",
        "source_sha256": "c" * 64,
        "configuration": {"deployment_sha": SHA, "deploy_root": "/opt/custom", "files": []},
        "secret_ciphertext": harness.secrets.encrypt(
            "profile:custom", "deployment_profile", json.dumps(custom_assets)
        ),
    }
    result = await harness.restart().create(request(slots=1))
    metadata = harness.repository.profiles[result["profiles"][0]["id"]]["configuration"][
        "automatic_setup"
    ]
    assert metadata["reverse_port"] not in {first_port, first_port + 1}


@pytest.mark.asyncio
async def test_concurrent_identical_requests_reuse_the_winners_saved_keys(harness: Harness) -> None:
    harness.repository.yield_on_insert = True
    first, second = await asyncio.gather(
        harness.restart().create(request()), harness.restart().create(request())
    )
    assert first == second
    assert len(harness.repository.profiles) == 2
    assert len(harness.tunnels.authorizations) == 6


@pytest.mark.asyncio
async def test_concurrent_different_inputs_for_one_request_admit_only_the_winner(
    harness: Harness,
) -> None:
    harness.repository.yield_on_insert = True
    outcomes = await asyncio.gather(
        harness.restart().create(request(slots=1)),
        harness.restart().create(request(slots=1, dns_server="9.9.9.9")),
        return_exceptions=True,
    )
    successes = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    refusals = [outcome for outcome in outcomes if isinstance(outcome, ComputeRefusal)]
    assert len(successes) == len(refusals) == 1
    assert refusals[0].code == "idempotency_conflict"
    assert len(harness.repository.profiles) == 1
    assert len(harness.tunnels.authorizations) == 3


@pytest.mark.asyncio
async def test_concurrent_different_requests_do_not_share_a_colliding_route(
    harness: Harness,
) -> None:
    seen: dict[int, str] = {}
    for index in range(10000):
        candidate = f"request-{index}"
        offset = int(hashlib.sha256(candidate.encode()).hexdigest()[:8], 16) % 30000
        if offset in seen:
            first_id, second_id = seen[offset], candidate
            break
        seen[offset] = candidate
    else:
        raise AssertionError("No colliding request fixture found")
    harness.repository.yield_on_insert = True
    results = await asyncio.gather(
        harness.restart().create(request(request_id=first_id, slots=1)),
        harness.restart().create(request(request_id=second_id, slots=1)),
    )
    assert results[0]["profiles"][0]["id"] != results[1]["profiles"][0]["id"]
    assert (
        len(
            {
                row["configuration"]["automatic_setup"]["reverse_port"]
                for row in harness.repository.profiles.values()
            }
        )
        == 2
    )
