"""Persistence boundary for durable compute inventory and fenced operations."""

from __future__ import annotations

from typing import Any, Protocol


class ComputeResourceRepository(Protocol):
    """Local state only; no provider calls or plaintext credentials."""

    async def list_connections(self) -> list[dict[str, Any]]:
        """Return metadata only; ciphertext is restricted to trusted get_connection."""
        ...

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        """Read the trusted connection record, including encrypted secret material."""
        ...

    async def save_connection(
        self,
        *,
        connection_id: str,
        name: str,
        provider_id: str,
        configuration: dict[str, Any],
        secret_ciphertext: str,
        masked_summary: str,
        status: str = "unverified",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Create or compare-and-swap encrypted connection metadata."""
        ...

    async def create_allocation_operation(
        self,
        *,
        request_id: str,
        fingerprint: str,
        kind: str,
        allocation: dict[str, Any],
        parameters: dict[str, Any],
        requested_by: str | None,
    ) -> dict[str, Any]:
        """Atomically persist resource identity and its first operation intent."""
        ...

    async def create_operation(
        self,
        *,
        request_id: str,
        fingerprint: str,
        kind: str,
        allocation_id: str | None,
        parameters: dict[str, Any],
        requested_by: str | None,
    ) -> dict[str, Any]:
        """Persist an idempotent operation with immutable execution parameters."""
        ...

    async def get_allocation(self, allocation_id: str) -> dict[str, Any] | None:
        """Read one allocation and its stable node and Host bindings."""
        ...

    async def list_allocations(self) -> list[dict[str, Any]]:
        """Read all managed inventory from one local snapshot."""
        ...

    async def server_inventory(self) -> dict[str, Any]:
        """Read global placement and resource/Host distribution in one snapshot."""
        ...

    async def record_observation_error(
        self,
        *,
        allocation_id: str,
        message: str,
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        """A read failure preserves the last measured deadline and its age."""
        ...

    async def record_inventory(
        self,
        *,
        allocation_id: str,
        hosts: list[str],
        empty: bool,
        verified_node_ids: list[str] | None = None,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> bool:
        """Accept a trusted broker scan only against the exact fenced topology.

        Retirement closes bindings first. A scan must cover every node and its
        Host execution environments; an unbound node is unknown, never empty.
        """
        ...

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Read the durable state and recovery receipt of one operation."""
        ...

    async def list_operations(
        self, *, allocation_id: str | None = None, request_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Find recent operation state, including recovery by request ID."""
        ...

    async def update_policy(
        self,
        *,
        allocation_id: str,
        expected_revision: int,
        policy: dict[str, Any],
        management_mode: str | None = None,
    ) -> dict[str, Any]:
        """Compare-and-swap policy without bypassing a committed release."""
        ...

    async def observe_allocation(
        self,
        *,
        allocation_id: str,
        observation: dict[str, Any],
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Save a fenced observation, preserving a known deadline when renewal is unknown."""
        ...

    async def upsert_node(
        self,
        *,
        allocation_id: str,
        node: dict[str, Any],
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Observe an immutable node identity without rebinding retained execution state."""
        ...

    async def claim_operation(
        self, *, worker_id: str, lease_seconds: float = 120
    ) -> dict[str, Any] | None:
        """Claim one allocation at a time, recovering an expired call before newer intents.

        An expired call retains its step/result for query recovery. The lease
        cannot cancel a remote request; adapters must never blindly replay it.
        """
        ...

    async def renew_operation_lease(
        self, *, operation_id: str, generation: int, worker_id: str, lease_seconds: float = 120
    ) -> bool:
        """Extend only the still-current worker lease."""
        ...

    async def checkpoint_operation(
        self,
        *,
        operation_id: str,
        generation: int,
        worker_id: str,
        step: str,
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Merge a durable step receipt without releasing the external-call lease."""
        ...

    async def finish_operation(
        self,
        *,
        operation_id: str,
        generation: int,
        worker_id: str,
        state: str,
        step: str,
        message: str | None = None,
        result: dict[str, Any] | None = None,
        retry_after_seconds: float | None = None,
    ) -> bool:
        """Commit a fenced result and relinquish the external-call lease."""
        ...

    async def prepare_bootstrap_binding(
        self,
        *,
        node_id: str,
        host: str,
        operation_id: str,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind before token issuance; no runner-supplied cloud identity is trusted."""
        ...

    async def clear_bootstrap_hold(
        self,
        *,
        node_id: str,
        host: str,
        operation_id: str,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> bool:
        """Open an exact ready binding without clearing other restrictions."""
        ...

    async def set_admission_hold(
        self,
        *,
        allocation_id: str,
        purpose: str,
        source_id: str,
        scope: str | None,
        operation_id: str | None = None,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Controller-owned restrictions never mutate Host status or sibling holds."""
        ...

    async def begin_retirement(
        self,
        *,
        allocation_id: str,
        operation_id: str,
        generation: int | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        """Close new dependencies before observing retirement blockers."""
        ...

    async def retirement_impact(self, allocation_id: str) -> dict[str, Any]:
        """Read the complete allocation scope and retained-state blockers."""
        ...

    async def commit_release(
        self, *, allocation_id: str, operation_id: str, generation: int, worker_id: str
    ) -> dict[str, Any]:
        """Commit irreversible release only after atomic dependency validation."""
        ...

    async def cancel_operation(self, operation_id: str) -> dict[str, Any]:
        """Cancel a safe retirement intent and remove only its own restriction."""
        ...

    async def resolve_acquisition(self, *, operation_id: str, external_id: str) -> dict[str, Any]:
        """Record explicit administrator identification of an unknown acquisition.

        This never asserts ownership or success. The same operation resumes
        through provider inspection and scope validation before any deployment.
        """
        ...

    async def retry_operation(self, operation_id: str) -> dict[str, Any]:
        """Resume an operation from its durable step and original parameters."""
        ...
