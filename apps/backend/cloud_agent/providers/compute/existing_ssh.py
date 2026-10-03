"""Attach an existing machine without claiming ownership of its lifecycle."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from cloud_agent.ports.external.compute_provider import (
    AcquireRequest,
    AcquisitionResolution,
    ComputeProvider,
    NodeObservation,
    OperationIntent,
    ProviderCapabilities,
    ProviderError,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
    SshConnection,
    UnsupportedCapability,
)

if TYPE_CHECKING:
    from cloud_agent.ports.external.host_bootstrap import SshTransport


class ExistingSSHConfig(SshConnection):
    """Explicit existing-machine connection and verified machine identity."""

    expected_machine_identity: str | None = None


class ExistingSSHProvider(ComputeProvider):
    """Attach SSH-accessible machines without owning their external lifecycle."""

    def __init__(self, transport: SshTransport) -> None:
        self._transport = transport

    def capabilities(self) -> ProviderCapabilities:
        """Return the actions this adapter implements."""
        return ProviderCapabilities(acquisition_modes=("attach",))

    def validate_config(self, mode: str, config: dict[str, Any]) -> dict[str, Any]:
        """Validate a specific acquisition mode and return persistable parameters."""
        if mode != "attach":
            raise UnsupportedCapability(mode)
        return ExistingSSHConfig.model_validate(config).model_dump(mode="json", exclude_none=True)

    @staticmethod
    def connection(config: ExistingSSHConfig) -> SshConnection:
        """Extract the secret-reference-only SSH transport description."""
        return SshConnection.model_validate(
            config.model_dump(exclude={"expected_machine_identity"})
        )

    async def acquire(self, request: AcquireRequest, intent: OperationIntent) -> ProviderReceipt:
        """Execute the persisted acquisition intent and return its acknowledgement."""
        config = self.validate_config(request.mode, request.config)
        observation = await self.inspect(
            ResourceRef(external_id=intent.allocation_id, config=config)
        )
        return ProviderReceipt(
            outcome="confirmed", resource=observation.ref, observation=observation
        )

    async def recover_acquire(self, intent: OperationIntent) -> AcquisitionResolution:
        """Resolve a previous acquisition without blindly creating another resource."""
        if intent.acquisition is None:
            return AcquisitionResolution(outcome="unknown")
        receipt = await self.acquire(intent.acquisition, intent)
        return AcquisitionResolution(outcome="confirmed", receipt=receipt)

    async def inspect(self, ref: ResourceRef) -> ResourceObservation:
        """Observe the exact resource identity and its full known node scope."""
        config = ExistingSSHConfig.model_validate(ref.config)
        connection = self.connection(config)
        machine = (await self._transport.run(connection, "cat /etc/machine-id")).strip()
        if not re.fullmatch(r"[a-fA-F0-9]{32}", machine):
            raise ProviderError(
                "machine_identity_unavailable", "Machine identity could not be verified"
            )
        identity = hashlib.sha256(machine.lower().encode()).hexdigest()
        if config.expected_machine_identity and identity != config.expected_machine_identity:
            raise ProviderError(
                "machine_identity_changed",
                "Machine identity changed; explicit re-adoption is required",
            )
        observed_ref = ref.model_copy(
            update={"config": {**ref.config, "expected_machine_identity": identity}}
        )
        return ResourceObservation(
            ref=observed_ref,
            state="ready",
            raw_state="ssh_verified",
            observed_at=datetime.now(timezone.utc),
            lease_known=False,
            scope_complete=True,
            nodes=(
                NodeObservation(
                    provider_node_id=ref.external_id,
                    instance_identity=f"{ref.external_id}:{identity}",
                    machine_identity=identity,
                    display_name=config.address,
                    connection=connection,
                ),
            ),
        )
