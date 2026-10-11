"""Shared deployment over a verified connection, independent of resource vendors."""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import Field, SecretStr, field_validator

from cloud_agent.ports.external.compute_provider import ComputeValue, NodeObservation, SshConnection


def validate_sandbox_storage_path(value: str) -> str:
    """Require a dedicated absolute storage directory without traversal.

    The host verifies the directory is an existing mount before deploying.
    Docker and containerd keep their default paths and store their data in
    this mount's ``docker`` and ``containerd`` child directories.
    """
    if not re.fullmatch(r"/[a-zA-Z0-9/_-]+", value) or "//" in value:
        raise ValueError("Sandbox storage must be an absolute path without traversal")
    value = value.rstrip("/") or "/"
    system_roots = {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/lib64",
        "/media",
        "/mnt",
        "/opt",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
        "/var/lib",
    }
    protected_roots = (
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib64",
        "/proc",
        "/run",
        "/sbin",
        "/sys",
        "/usr",
        "/var/lib/docker",
        "/var/lib/containerd",
        "/var/lib/cloud-agent",
        "/var/lib/hybridinference",
    )
    if value in system_roots or any(
        value == root or value.startswith(root + "/") for root in protected_roots
    ):
        raise ValueError("Choose a dedicated sandbox storage directory")
    return value


class DeploymentProfile(ComputeValue):
    """Operator configuration only; selected by the control plane or an explicit ID.

    source_bundle is a trusted local git-archive tar for deployment_sha. Its
    digest is checked before upload. Environment and files are trusted secret
    references; cloud account and management SSH credentials are never copied.
    """

    id: str
    label: str
    source_bundle: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deploy_root: str = Field(default="/opt/cloud-agent", pattern=r"^/[a-zA-Z0-9/_-]+$")
    environment_ref: str
    files: dict[str, str] = Field(default_factory=dict)
    install_prerequisites: bool = True
    timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    # A provider-mounted local disk (the Cloud Agent CloudLab profile's
    # ``dataMount``) that holds Docker's and containerd's state under their
    # default paths. A Host's whole state is what moves between nodes.
    data_mount: str | None = Field(default=None, pattern=r"^/[a-zA-Z0-9/_-]+$")
    # A per-Host creation value takes precedence over the legacy profile mount.
    sandbox_storage_path: str | None = Field(default=None, max_length=4096)

    @field_validator("sandbox_storage_path")
    @classmethod
    def validate_storage_path(cls, value: str | None) -> str | None:
        """Validate storage paths both when configured and when loaded from storage."""
        return None if value is None else validate_sandbox_storage_path(value)

    @field_validator("deploy_root")
    @classmethod
    def validate_root(cls, value: str) -> str:
        """Require a dedicated deployment directory, never a system root."""
        if value.rstrip("/") in {"", "/opt", "/srv", "/usr", "/var", "/home", "/root"}:
            raise ValueError("Choose a dedicated deployment directory")
        return value.rstrip("/")

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject absolute paths and traversal in trusted deployment file names."""
        for name in value:
            if (
                not name
                or name.startswith("/")
                or any(p in {"", ".", ".."} for p in name.split("/"))
            ):
                raise ValueError("Deployment file names must be relative paths")
        return value


class BootstrapIntent(ComputeValue):
    """Stable binding to be authorized before issuing an enrollment token.

    ``namespace`` is the Compose project that names every retained sandbox,
    group bridge and nftables table. It defaults to ``ca-<node>``; a node that
    takes over a rotated Host keeps the Host's original namespace.
    """

    operation_id: str
    allocation_id: str
    node_id: str
    host: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    namespace: str | None = Field(default=None, pattern=r"^ca-[a-z0-9]{1,61}$")

    @property
    def project(self) -> str:
        """The Compose project this deployment runs under."""
        return self.namespace or default_namespace(self.node_id)


def default_namespace(node_id: str) -> str:
    """The sandbox namespace of a Host started on this node."""
    return "ca-" + node_id.replace("-", "").lower()


class TransferResult(ComputeValue):
    """What one node-to-node copy of a Host's container state moved."""

    bytes_transferred: int = 0
    seconds: float = 0.0
    streams: int = 1


class PreflightResult(ComputeValue):
    """Machine identity and supported-platform checks before installation."""

    ready: bool
    machine_identity: str
    operating_system: str
    architecture: str
    checks: dict[str, bool]
    blocked_reasons: tuple[str, ...] = ()


class BootstrapResult(ComputeValue):
    """Local deployment readiness; pool admission also needs heartbeat evidence."""

    ready: bool
    host: str
    deployment_sha: str
    blocked_reasons: tuple[str, ...] = ()


class HostInventory(ComputeValue):
    """Fresh machine-side evidence for release guards, including stopped state."""

    complete: bool
    retained_containers: int = 0
    unknown_containers: int = 0
    orphan_state: bool = False
    blocked_reasons: tuple[str, ...] = ()


class SshTransport(Protocol):
    """Execute trusted scripts through a host-key-verified SSH connection."""

    async def run(
        self,
        connection: SshConnection,
        script: str,
        *,
        stdin: bytes | None = None,
        timeout: float = 30,
    ) -> str:
        """Run a trusted remote command without exposing credentials or error output."""
        ...


class HostBootstrap(Protocol):
    """Shared host deployment and verification independent of the cloud vendor."""

    async def preflight(self, node: NodeObservation, profile: DeploymentProfile) -> PreflightResult:
        """Verify machine identity and prerequisites without installing software."""
        ...

    async def deploy(
        self,
        node: NodeObservation,
        profile: DeploymentProfile,
        intent: BootstrapIntent,
        enrollment_token: SecretStr | None,
        *,
        prerequisites: bool | None = None,
        reenroll: bool = False,
    ) -> BootstrapResult:
        """Install the trusted host recipe while retaining its existing safety gates.

        ``prerequisites=False`` skips package and Kata installation on a node
        staged earlier. ``reenroll`` discards the node's persisted Host
        credential so the supplied token enrolls it again.
        """
        ...

    async def readiness(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> BootstrapResult:
        """Recheck deployment identity and the full host and broker readiness gates."""
        ...

    async def inventory(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> HostInventory:
        """Observe stopped containers and orphan state for resource release guards."""
        ...

    async def stage(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> BootstrapResult:
        """Install prerequisites and the recipe without starting or enrolling a Host."""
        ...

    async def stop(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> None:
        """Stop the Host stack, its sandboxes and the container daemons, keeping all state."""
        ...

    async def transfer(
        self,
        source: NodeObservation,
        target: NodeObservation,
        target_profile: DeploymentProfile,
        target_intent: BootstrapIntent,
        *,
        final: bool,
        first: bool = False,
    ) -> TransferResult:
        """Copy Docker's and containerd's state directly from source to target.

        ``first`` empties the staged target's fresh daemons before the copy;
        ``final`` runs on a stopped source and deletes what it no longer has.
        """
        ...

    async def revoke_transfer(self, source: NodeObservation, target: NodeObservation) -> None:
        """Remove the one-off transfer key from both machines."""
        ...

    async def wipe(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> None:
        """Delete a node's container state after its successor took the Host over."""
        ...
