"""Shared deployment over a verified connection, independent of resource vendors."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field, SecretStr, field_validator

from cloud_agent.ports.external.compute_provider import ComputeValue, NodeObservation, SshConnection


class DeploymentProfile(ComputeValue):
    """Operator configuration only; the browser selects ``id`` from a catalog.

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
    """Stable binding to be authorized before issuing an enrollment token."""

    operation_id: str
    allocation_id: str
    node_id: str
    host: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")


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
    ) -> BootstrapResult:
        """Install the trusted host recipe while retaining its existing safety gates."""
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
