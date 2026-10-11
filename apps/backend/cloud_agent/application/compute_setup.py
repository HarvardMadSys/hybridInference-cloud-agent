"""Prepare encrypted, recoverable Host setups from the deployed release.

Only an explicit administrator action provisions infrastructure access. Pending
profiles checkpoint generated keys before SSH changes and remain unavailable to
allocation until every tunnel is provisioned. Retries reuse those exact inputs.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

from cloud_agent.domain.compute import ComputeRefusal, fingerprint
from cloud_agent.ports.external.compute_provider import SshConnection
from cloud_agent.providers.compute.setup_tunnels import (
    ControlPlaneEndpoint,
    generate_tunnel_key,
    tunnel_username,
)
from cloud_agent.settings import text_setting

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent.compute_config import ComputeConfiguration, ComputeSecretResolver
    from cloud_agent.compute_secrets import ComputeSecretStore
    from cloud_agent.ports.compute_resources import ComputeResourceRepository
    from cloud_agent.providers.compute.setup_tunnels import SshTunnelSetup
    from cloud_agent.schemas_compute import AutomaticDeploymentSetup, SetupMachine


@dataclass(frozen=True)
class HostRelease:
    """One immutable, build-produced archive shared by every route slot."""

    sha: str
    digest: str
    bundle: bytes


class AutomaticComputeSetup:
    """Generate setup inputs without exposing them through the administrator API."""

    def __init__(
        self,
        *,
        repository: ComputeResourceRepository,
        secrets: ComputeSecretStore,
        resolver: ComputeSecretResolver,
        configuration: ComputeConfiguration,
        tunnels: SshTunnelSetup,
        validate_bundle: Callable[[bytes], None],
        release_directory: Path = Path("/app/compute-host-release"),
    ) -> None:
        self.repository = repository
        self.secrets = secrets
        self.resolver = resolver
        self.configuration = configuration
        self.tunnels = tunnels
        self.validate_bundle = validate_bundle
        self.release_directory = release_directory

    def _release(self) -> HostRelease:
        try:
            manifest_path = self.release_directory / "release.json"
            if manifest_path.is_symlink() or manifest_path.stat().st_size > 4096:
                raise ValueError
            manifest = json.loads(manifest_path.read_text())
            sha = manifest["source_commit"]
            digest = manifest["source_sha256"]
            if (
                manifest.get("format_version") != 1
                or manifest.get("bundle") != "source.tar.gz"
                or not isinstance(sha, str)
                or not re.fullmatch(r"[a-f0-9]{40}", sha)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
            ):
                raise ValueError
            bundle_path = self.release_directory / "source.tar.gz"
            if bundle_path.is_symlink() or not 0 < bundle_path.stat().st_size <= 64 * 1024 * 1024:
                raise ValueError
            bundle = bundle_path.read_bytes()
            if hashlib.sha256(bundle).hexdigest() != digest:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            raise ComputeRefusal(
                "automatic_release_unavailable",
                "Automatic setup needs the Host bundle included in the control-plane release. Update the deployment or use advanced manual setup.",
                status=503,
            ) from None
        if sha != os.environ.get("AGENT_DEPLOYMENT_SHA", ""):
            raise ComputeRefusal(
                "automatic_release_mismatch",
                "The Host bundle does not match the running control-plane release. Update the deployment before preparing servers.",
                status=503,
            )
        self.validate_bundle(bundle)
        return HostRelease(sha, digest, bundle)

    def status(self) -> dict[str, Any]:
        """Return only release readiness and actionable non-secret prerequisites."""
        sha = None
        blocked = []
        try:
            sha = self._release().sha
        except ComputeRefusal as exc:
            blocked.append({"code": exc.code, "message": str(exc)})
        if not self.secrets.configured:
            blocked.append(
                {
                    "code": "credential_unavailable",
                    "message": "Configure the compute credential encryption key in this deployment.",
                }
            )
        if not text_setting("workspace_broker_token"):
            blocked.append(
                {
                    "code": "broker_credential_missing",
                    "message": "Configure the existing workspace broker credential in Admin Settings.",
                }
            )
        return {"available": not blocked, "deployment_sha": sha, "blocked_reasons": blocked}

    async def read_status(self) -> dict[str, Any]:
        """Restore non-secret infrastructure inputs and interrupted request identity."""
        status = self.status()
        saved = await self.repository.provider_defaults("automatic_setup")
        request = saved.get("request")
        if not isinstance(request, dict):
            return status
        defaults = {key: value for key, value in request.items() if key != "request_id"}
        status["defaults"] = defaults
        status["pending_request_id"] = None
        if saved.get("source_commit") == status["deployment_sha"]:
            request_id = request.get("request_id")
            rows = [
                row
                for row in await self.repository.list_deployment_profiles()
                if row["configuration"].get("automatic_setup", {}).get("request_id") == request_id
            ]
            if len(rows) != defaults.get("slots") or not all(
                row["configuration"]["automatic_setup"].get("ready") for row in rows
            ):
                status["pending_request_id"] = request_id
        return status

    async def _connection(self, machine: SetupMachine) -> SshConnection:
        row = await self.repository.get_connection(machine.connection_id)
        if row is None or row["provider_id"] != "existing_ssh":
            raise ComputeRefusal(
                "ssh_configuration",
                "Choose an existing SSH connection for each stable machine.",
                status=422,
            )
        if row.get("status") != "verified":
            raise ComputeRefusal(
                "ssh_configuration",
                "Validate the selected SSH connections before preparing servers.",
                status=422,
            )
        connection = SshConnection(
            address=machine.address,
            port=machine.port,
            username=machine.username,
            credential_ref=machine.connection_id,
            known_hosts_ref=machine.connection_id + ":known_hosts",
        )
        # Resolve only the explicitly selected connection's verified trust file.
        # OpenSshTransport authenticates it before running any infrastructure step.
        if not (await self.resolver.resolve(connection.known_hosts_ref or "")).strip():
            raise ComputeRefusal(
                "ssh_configuration", "Verified SSH host keys are required.", status=422
            )
        return connection

    async def _reserved_ports(self, bind: str) -> set[int]:
        """Honor legacy and custom routes as well as generated route identities."""
        profiles = list(self.configuration.profiles.values())
        rows = await self.repository.list_deployment_profiles()
        ports = set()
        for row in rows:
            automatic = row["configuration"].get("automatic_setup", {})
            if automatic.get("endpoint", {}).get("gateway_ip") == bind:
                ports.add(int(automatic["reverse_port"]))
            elif not automatic:
                template = await self.resolver.resolve(f"stored-profile:{row['id']}:environment")
                values = dotenv_values(stream=io.StringIO(template), interpolate=False)
                if values.get("AGENT_BROKER_REVERSE_BIND") == bind:
                    ports.add(int(values.get("AGENT_BROKER_REVERSE_PORT") or 0))
        for profile in profiles:
            template = await self.resolver.resolve(profile.environment_ref)
            values = dotenv_values(stream=io.StringIO(template), interpolate=False)
            if values.get("AGENT_BROKER_REVERSE_BIND") == bind:
                ports.add(int(values.get("AGENT_BROKER_REVERSE_PORT") or 0))
        return ports

    def _assets(
        self,
        request: AutomaticDeploymentSetup,
        endpoint: ControlPlaneEndpoint,
        profile_id: str,
        reverse_port: int,
        gateway_hosts: str,
        control_hosts: str,
    ) -> dict[str, Any]:
        environment = {
            "AGENT_SANDBOX_IMAGE": "cloud-agent-sandbox:" + profile_id,
            "AGENT_WORKSPACE_BROKER_TOKEN": text_setting("workspace_broker_token"),
            "AGENT_BROKER_REVERSE_BIND": endpoint.gateway_ip,
            "AGENT_BROKER_REVERSE_PORT": str(reverse_port),
            "AGENT_WORKSPACE_BROKER_ADVERTISE_URL": f"http://{endpoint.gateway_ip}:{reverse_port}",
            "AGENT_EGRESS_CHAT_NETWORK_POOL": request.chat_network_pool,
            "AGENT_EGRESS_DNS_SERVER": request.dns_server,
            "AGENT_EGRESS_PROTECTED_CIDRS": ",".join(request.protected_cidrs),
            "AGENT_HOST_SLOTS": "3",
            "AGENT_SHARED_RESIDENT_CAPACITY": "8",
            "AGENT_RETAINED_DOCKER_DATA_ROOT": "/var/lib/docker",
            "AGENT_HOST_STORAGE_PROBE": "overlay_root",
        }
        files = {}
        public_keys = {}
        for role, prefix, machine, port, hosts in (
            ("gateway", "AGENT_TUNNEL", request.gateway, request.gateway_port, gateway_hosts),
            ("mcp", "AGENT_MCP_TUNNEL", request.control, endpoint.mcp_port, control_hosts),
            (
                "control",
                "AGENT_CONTROL_PLANE_TUNNEL",
                request.control,
                endpoint.main_port,
                control_hosts,
            ),
        ):
            key = generate_tunnel_key()
            files[f".compute-secrets/{role}.key"] = key.private_key.get_secret_value()
            files[f".compute-secrets/{role}.known_hosts"] = hosts
            public_keys[role] = key.public_key
            environment.update(
                {
                    prefix
                    + "_SSH_DESTINATION": f"{tunnel_username(profile_id, 'inference' if role == 'gateway' else role)}@{machine.address}",
                    prefix + "_SSH_PORT": str(machine.port),
                    prefix + "_SSH_KEY": f"{{deploy_root}}/.compute-secrets/{role}.key",
                    prefix
                    + "_SSH_KNOWN_HOSTS": f"{{deploy_root}}/.compute-secrets/{role}.known_hosts",
                    prefix + "_REMOTE_HOST": "127.0.0.1",
                    prefix + "_REMOTE_PORT": str(port),
                }
            )
        # Match the Host bootstrap's single-line dotenv contract before storing.
        if any(
            any(char in value for char in ("\n", "\r", "'", "\x00"))
            for value in environment.values()
        ):
            raise ComputeRefusal(
                "invalid_configuration",
                "Host configuration values must be single-line dotenv data.",
                status=422,
            )
        return {
            "environment": "".join(f"{name}='{value}'\n" for name, value in environment.items()),
            "files": files,
            "public_keys": public_keys,
        }

    @staticmethod
    def _validate_endpoint(metadata: dict[str, Any], endpoint: ControlPlaneEndpoint) -> None:
        if metadata.get("endpoint") != endpoint.model_dump(mode="json"):
            raise ComputeRefusal(
                "automatic_infrastructure_changed",
                "The stable control-plane route changed. Prepare a new setup rather than changing saved deployment inputs.",
                status=409,
            )

    async def _reserve(
        self,
        request: AutomaticDeploymentSetup,
        release: HostRelease,
        endpoint: ControlPlaneEndpoint,
        request_fingerprint: str,
        slot: int,
        reserved: set[int],
        gateway_hosts: str,
        control_hosts: str,
    ) -> dict[str, Any]:
        first = int(hashlib.sha256(request.request_id.encode()).hexdigest()[:8], 16) % 30000
        for offset in range(30000):
            reverse_port = 20000 + (first + slot + offset) % 30000
            route = f"{endpoint.machine_identity}:{endpoint.gateway_ip}:{reverse_port}"
            profile_id = "auto-" + hashlib.sha256(route.encode()).hexdigest()[:24]
            existing = await self.repository.get_deployment_profile(profile_id)
            if existing is not None:
                metadata = existing["configuration"].get("automatic_setup", {})
                if (
                    metadata.get("request_id") == request.request_id
                    and metadata.get("fingerprint") == request_fingerprint
                    and metadata.get("slot") == slot
                ):
                    self._validate_endpoint(metadata, endpoint)
                    return existing
                continue
            if reverse_port in reserved or reverse_port in {
                endpoint.main_port,
                endpoint.mcp_port,
            }:
                continue
            assets = self._assets(
                request, endpoint, profile_id, reverse_port, gateway_hosts, control_hosts
            )
            configuration = {
                "deployment_sha": release.sha,
                "deploy_root": "/opt/cloud-agent-" + profile_id,
                "files": list(assets["files"]),
                "install_prerequisites": True,
                "timeout_seconds": 3600,
                "automatic_setup": {
                    "request_id": request.request_id,
                    "fingerprint": request_fingerprint,
                    "slot": slot,
                    "slots": request.slots,
                    "ready": False,
                    "endpoint": endpoint.model_dump(mode="json"),
                    "reverse_port": reverse_port,
                },
            }
            try:
                row = await self.repository.save_deployment_profile(
                    profile_id=profile_id,
                    label=f"Automatic Cloud Agent server {slot + 1}",
                    configuration=configuration,
                    source_bundle=release.bundle,
                    source_sha256=release.digest,
                    secret_ciphertext=self.secrets.encrypt(
                        "profile:" + profile_id, "deployment_profile", json.dumps(assets)
                    ),
                )
            except ValueError as exc:
                if str(exc) != "deployment_profile_exists":
                    raise
                # Another API replica reserved this route. Reuse its inputs
                # only for the same request; another request keeps its route.
                matching = [
                    item
                    for item in await self.repository.list_deployment_profiles()
                    if item["configuration"].get("automatic_setup", {}).get("request_id")
                    == request.request_id
                    and item["configuration"]["automatic_setup"].get("slot") == slot
                ]
                winner = await self.repository.get_deployment_profile(
                    matching[0]["id"] if matching else profile_id
                )
                metadata = (winner or {}).get("configuration", {}).get("automatic_setup", {})
                if (
                    metadata.get("request_id") == request.request_id
                    and metadata.get("fingerprint") != request_fingerprint
                ):
                    raise ComputeRefusal(
                        "idempotency_conflict",
                        "This setup request already has different inputs. Start a new setup request.",
                        status=409,
                    ) from None
                if (
                    winner is not None
                    and metadata.get("request_id") == request.request_id
                    and metadata.get("fingerprint") == request_fingerprint
                    and metadata.get("slot") == slot
                ):
                    self._validate_endpoint(metadata, endpoint)
                    return winner
                continue
            reserved.add(reverse_port)
            return row
        raise ComputeRefusal(
            "automatic_capacity_unavailable",
            "No unreserved broker route remains for this stable machine.",
            status=409,
        )

    async def create(self, request: AutomaticDeploymentSetup) -> dict[str, Any]:
        """Checkpoint exact inputs, provision scoped access, and activate all slots."""
        release = self._release()
        status = self.status()
        if not status["available"]:
            blocked = status["blocked_reasons"][0]
            raise ComputeRefusal(blocked["code"], blocked["message"], status=503)
        request_fingerprint = fingerprint(
            "automatic_setup",
            {**request.model_dump(mode="json"), "release": release.sha, "bundle": release.digest},
        )
        all_profiles = await self.repository.list_deployment_profiles()
        checkpoints = {}
        for row in all_profiles:
            metadata = row["configuration"].get("automatic_setup", {})
            if metadata.get("request_id") != request.request_id:
                continue
            if metadata.get("fingerprint") != request_fingerprint:
                raise ComputeRefusal(
                    "idempotency_conflict",
                    "This setup request already has different inputs. Start a new setup request.",
                    status=409,
                )
            checkpoints[metadata["slot"]] = row
        if len(checkpoints) == request.slots and all(
            row["configuration"]["automatic_setup"]["ready"] for row in checkpoints.values()
        ):
            return {
                "profiles": [
                    {"id": row["id"], "label": row["label"]}
                    for _, row in sorted(checkpoints.items())
                ]
            }
        gateway = await self._connection(request.gateway)
        control = await self._connection(request.control)
        endpoint = await self.tunnels.discover_control_plane(control, release.sha)
        allowed = text_setting("broker_url_allowed_hosts").strip()
        if allowed and endpoint.gateway_ip not in {host.strip() for host in allowed.split(",")}:
            raise ComputeRefusal(
                "broker_route_not_allowed",
                "The detected control-plane bridge address must be included in the workspace broker host allowlist.",
                status=422,
            )
        gateway_identity = await self.tunnels.check_gateway(gateway, request.gateway_port)
        if gateway_identity == endpoint.machine_identity and request.gateway_port in {
            endpoint.main_port,
            endpoint.mcp_port,
        }:
            raise ComputeRefusal(
                "automatic_listener_conflict",
                "The gateway must use a different port from the control-plane API and MCP listeners on the same machine.",
                status=422,
            )
        await self.repository.set_provider_defaults(
            "automatic_setup",
            {"request": request.model_dump(mode="json"), "source_commit": release.sha},
        )
        gateway_hosts = await self.resolver.resolve(gateway.known_hosts_ref or "")
        control_hosts = await self.resolver.resolve(control.known_hosts_ref or "")
        reserved = await self._reserved_ports(endpoint.gateway_ip)
        reserved.update({endpoint.main_port, endpoint.mcp_port})
        rows = []
        for slot in range(request.slots):
            saved = checkpoints.get(slot)
            if saved is None:
                saved = await self._reserve(
                    request,
                    release,
                    endpoint,
                    request_fingerprint,
                    slot,
                    reserved,
                    gateway_hosts,
                    control_hosts,
                )
            else:
                self._validate_endpoint(saved["configuration"]["automatic_setup"], endpoint)
            rows.append(saved)
        # All private keys exist encrypted in durable storage before any SSH
        # authorization changes. A lost response can be retried with the same ID.
        for row in rows:
            full = await self.repository.get_deployment_profile(row["id"])
            if full is None:
                raise ComputeRefusal(
                    "automatic_setup_unavailable",
                    "The saved server setup is unavailable.",
                    status=503,
                )
            assets = json.loads(
                self.secrets.decrypt(
                    "profile:" + row["id"], "deployment_profile", full["secret_ciphertext"]
                )
            )
            keys = assets["public_keys"]
            await self.tunnels.provision_gateway(
                gateway, keys["gateway"], request.gateway_port, row["id"]
            )
            await self.tunnels.provision_control(
                control,
                keys["mcp"],
                keys["control"],
                endpoint,
                row["configuration"]["automatic_setup"]["reverse_port"],
                row["id"],
            )
        await self.repository.activate_automatic_profiles(
            profile_ids=[row["id"] for row in rows],
            request_id=request.request_id,
            request_fingerprint=request_fingerprint,
        )
        return {"profiles": [{"id": row["id"], "label": row["label"]} for row in rows]}
