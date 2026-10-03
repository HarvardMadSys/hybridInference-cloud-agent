"""Vendor-independent resource acquisition. No pool or scheduling dependencies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime  # noqa: TC003 - Pydantic resolves this annotation at runtime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class ComputeValue(BaseModel):
    """Immutable, JSON-serializable value at the provider boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SecretResolver(Protocol):
    """Resolve trusted secret references on the control plane only."""

    async def resolve(self, ref: str) -> str:
        """Return the secret for an authorized reference without logging it."""
        ...


class ProviderCapabilities(ComputeValue):
    """Declare supported acquisition and lifecycle actions."""

    acquisition_modes: tuple[Literal["attach", "provision"], ...]
    can_extend: bool = False
    can_release: bool = False


class AcquireRequest(ComputeValue):
    """An explicit attach or provision intent and validated vendor parameters."""

    mode: Literal["attach", "provision"]
    config: dict[str, Any]


class OperationIntent(ComputeValue):
    """Persisted operation identity for retries and uncertain-result recovery."""

    operation_id: str
    allocation_id: str
    requested_at: datetime
    request_fingerprint: str
    acquisition: AcquireRequest | None = None
    known_external_id: str | None = None


class ResourceRef(ComputeValue):
    """Immutable external identity plus validated connection references."""

    external_id: str
    config: dict[str, Any]


class SshConnection(ComputeValue):
    """Verified SSH destination with references rather than credential values."""

    address: str = Field(min_length=1, max_length=253, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.:-]*$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]*$")
    credential_ref: str = Field(min_length=1)
    known_hosts_ref: str = Field(min_length=1)


class NodeObservation(ComputeValue):
    """One node identity within an allocation and its access description."""

    provider_node_id: str
    instance_identity: str
    machine_identity: str | None = None
    display_name: str
    connection: SshConnection | None = None


class ResourceObservation(ComputeValue):
    """Provider facts, with independent lease and complete-scope evidence."""

    ref: ResourceRef
    state: Literal["pending", "ready", "failed", "terminated", "unknown"]
    raw_state: str
    observed_at: datetime
    expires_at: datetime | None = None
    lease_known: bool = False
    nodes: tuple[NodeObservation, ...] = ()
    scope_complete: bool = False


class ProviderReceipt(ComputeValue):
    """An acknowledgement that does not imply the requested state is ready."""

    outcome: Literal["confirmed", "pending", "unknown"]
    resource: ResourceRef | None = None
    observation: ResourceObservation | None = None


class AcquisitionResolution(ComputeValue):
    """Recovery evidence; unknown must not trigger another create."""

    outcome: Literal["confirmed", "pending", "not_created", "unknown"]
    receipt: ProviderReceipt | None = None


class ExtendRequest(ComputeValue):
    """A fixed absolute lease target, reused unchanged on retries."""

    target_expires_at: datetime
    reason: str = Field(min_length=1, max_length=2048)


class ProviderError(Exception):
    """A deliberately safe diagnostic; never include HTTP/SSH output or secrets."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class UnsupportedCapability(ProviderError):
    """Reject unsupported actions explicitly instead of pretending success."""

    def __init__(self, capability: str) -> None:
        super().__init__("unsupported_capability", f"Provider does not support {capability}")


class ComputeProvider(ABC):
    """Acquire and observe resources without deploying or scheduling workloads."""

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        """Return the actions this adapter implements."""
        ...

    @abstractmethod
    def validate_config(self, mode: str, config: dict[str, Any]) -> dict[str, Any]:
        """Validate a specific acquisition mode and return persistable parameters."""
        ...

    @abstractmethod
    async def acquire(self, request: AcquireRequest, intent: OperationIntent) -> ProviderReceipt:
        """Execute the persisted acquisition intent and return its acknowledgement."""
        ...

    @abstractmethod
    async def recover_acquire(self, intent: OperationIntent) -> AcquisitionResolution:
        """Resolve a previous acquisition without blindly creating another resource."""
        ...

    @abstractmethod
    async def inspect(self, ref: ResourceRef) -> ResourceObservation:
        """Observe the exact resource identity and its full known node scope."""
        ...

    async def extend(self, ref: ResourceRef, request: ExtendRequest) -> ProviderReceipt:
        """Request the fixed expiration target and preserve its actual observed result."""
        raise UnsupportedCapability("extend")

    async def release(self, ref: ResourceRef, intent: OperationIntent) -> ProviderReceipt:
        """Release the exact resource after orchestration commits its safety checks.

        Replaying the same ResourceRef and OperationIntent must never target a
        replacement resource, widen scope, or add a second destructive effect.
        Adapters must support query-before-replay of this immutable identity;
        absence is success only with authenticated evidence in that scope.
        """
        raise UnsupportedCapability("release")
