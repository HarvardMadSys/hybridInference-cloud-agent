"""Persistence boundary for durable compute inventory and fenced operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class ComputeResourceRepository(Protocol):
    """Local state only; no provider calls or plaintext credentials."""

    async def list_deployment_profiles(self) -> list[dict[str, Any]]:
        """List profile metadata without bundles, plaintext inputs or ciphertext."""
        ...

    async def get_deployment_profile(self, profile_id: str) -> dict[str, Any] | None:
        """Read one trusted profile including its bundle and encrypted inputs."""
        ...

    async def save_deployment_profile(
        self,
        *,
        profile_id: str,
        label: str,
        configuration: dict[str, Any],
        source_bundle: bytes,
        source_sha256: str,
        secret_ciphertext: str,
    ) -> dict[str, Any]:
        """Create one profile. A host release may later advance its commit and bundle."""
        ...

    async def advance_host_release(
        self,
        *,
        allocation_id: str,
        previous_sha: str,
        previous_digest: str,
        deployment_sha: str,
        source_sha256: str,
        source_bundle: bytes,
        release_bundle_path: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Move one release's stored bundles and live snapshots onto a new commit.

        Profiles and allocations that another deployment is already using stay
        on their current bytes. Tunnel credentials and install directories stay.
        """
        ...

    async def provider_defaults(self, provider_id: str) -> dict[str, Any]:
        """Return the saved non-secret form defaults for one provider."""
        ...

    async def activate_automatic_profiles(
        self,
        *,
        profile_ids: list[str],
        request_id: str,
        request_fingerprint: str,
    ) -> None:
        """Atomically admit provisioned slots while keeping release inputs immutable."""
        ...

    async def set_provider_defaults(
        self, provider_id: str, defaults: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace one provider's defaults without disturbing other providers."""
        ...

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

    async def remove_allocation(self, *, allocation_id: str, expected_revision: int) -> None:
        """Atomically remove safe server inventory and its private bookkeeping."""
        ...

    async def list_allocations(self) -> list[dict[str, Any]]:
        """Read all managed inventory from one local snapshot."""
        ...

    async def server_inventory(self) -> dict[str, Any]:
        """Read global placement and resource/Host distribution in one snapshot."""
        ...

    async def get_cloudlab_autoscaling(self) -> dict[str, Any]:
        """Read the singleton policy, fenced runtime, owned members and reservations."""
        ...

    async def update_cloudlab_autoscaling(
        self,
        *,
        expected_revision: int,
        policy: dict[str, Any],
        profile_snapshots: dict[str, dict[str, Any]],
        requested_by: str | None,
    ) -> dict[str, Any]:
        """Compare-and-swap policy and its profile reservations atomically."""
        ...

    async def list_reserved_deployment_profiles(self) -> list[str]:
        """List profile IDs reserved from administrator-created allocations."""
        ...

    async def create_autoscale_allocation_operation(
        self,
        *,
        expected_revision: int,
        expected_generation: int,
        request_id: str,
        fingerprint: str,
        allocation: dict[str, Any],
        parameters: dict[str, Any],
        requested_by: str | None,
        decision: dict[str, Any],
        cooldown_seconds: int,
    ) -> dict[str, Any]:
        """Reserve one free policy profile and atomically queue one owned acquire."""
        ...

    async def assert_autoscale_acquire_connections_verified(
        self,
        *,
        allocation_id: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Fence the final credential-health check before an owned provider acquire."""
        ...

    async def queue_autoscale_retire(
        self,
        *,
        expected_revision: int,
        expected_generation: int,
        allocation_id: str,
        request_id: str,
        fingerprint: str,
        parameters: dict[str, Any],
        requested_by: str | None,
        decision: dict[str, Any],
        cooldown_seconds: int,
    ) -> dict[str, Any]:
        """Queue retirement only for an owned allocation and advance runtime fencing."""
        ...

    async def update_cloudlab_autoscaling_runtime(
        self,
        *,
        expected_generation: int,
        runtime: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare-and-swap hysteresis, observations and a non-action decision."""
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
        """Accept a trusted sandbox manager scan only against the exact fenced topology.

        Retirement closes bindings first. A scan must cover every node and its
        Host execution environments; an unbound node is unknown, never empty.
        """
        ...

    async def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Read the durable state and recovery receipt of one operation."""
        ...

    async def list_operations(
        self,
        *,
        allocation_id: str | None = None,
        request_id: str | None = None,
        kinds: list[str] | None = None,
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

    async def create_node_registration(
        self,
        *,
        allocation_id: str,
        token_hash: str,
        ttl_seconds: float,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> dict[str, Any]:
        """Open the one registration a new node may complete; only the hash is kept."""
        ...

    async def complete_node_registration(self, *, token_hash: str, host_keys: list[str]) -> str:
        """Record a node's host keys once: registered, replayed, conflict or invalid.

        Only the newest registration of a live allocation is open. The same
        keys again get the same answer; a different set is never swapped in.
        """
        ...

    async def node_registration(self, *, allocation_id: str) -> dict[str, Any] | None:
        """Read the newest registration of an allocation, without its token."""
        ...

    async def record_host_key(
        self,
        *,
        node_id: str,
        host_key: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Pin the host key a node registered, for this exact node instance only."""
        ...

    async def claim_operation(
        self, *, worker_id: str, lease_seconds: float = 120
    ) -> dict[str, Any] | None:
        """Claim one allocation at a time, recovering an expired call before newer intents.

        An expired call retains its step/result for query recovery. The lease
        cannot cancel a remote request; adapters must never blindly replay it.
        A waiting extension may coexist with recovery rotation: it only changes
        the old resource's deadline, and no local provider call is still running.
        Other mutations, especially retirement, still wait for confirmation.
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

    async def apply_cloudlab_autoscaling_slot_target(
        self,
        *,
        expected_revision: int,
        expected_generation: int,
        allocation_id: str,
        host: str | None,
        target_slots: int | None,
        capacity_sampled_at: datetime | None,
        capacity_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Fence an owned Host's slot target and its all-claims admission hold.

        Recheck exact policy/runtime, binding, readiness and capacity sample.
        Refusals keep the sizing hold closed.
        """
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

    async def running_attempts(self, *, host: str) -> int:
        """Count the turns a Host is executing now."""
        ...

    async def create_rotation_candidate(
        self,
        *,
        source_allocation_id: str,
        allocation: dict[str, Any],
        request_id: str,
        fingerprint: str,
        parameters: dict[str, Any],
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> dict[str, Any]:
        """Persist a replacement node's allocation and its staging acquisition together.

        Only the rotation of a live allocation whose node runs the named Host
        may create one. The replacement carries the Host identity it will take
        over and never shares a deployment profile with a live allocation.
        """
        ...

    async def abandon_candidate(
        self,
        *,
        allocation_id: str,
        request_id: str,
        fingerprint: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> dict[str, Any] | None:
        """Stop a replacement node's pending work and queue its release.

        Returns the release operation, or None while the candidate's own work is
        executing or its outcome is unknown and it cannot be stopped safely.
        """
        ...

    async def commit_candidate_release(
        self, *, allocation_id: str, operation_id: str, generation: int, worker_id: str
    ) -> dict[str, Any]:
        """Commit the release of a replacement node that never took its Host over.

        It holds only a copy of a Host that still runs elsewhere, so release
        needs no retained-state inventory — but only while no Host is bound to
        its nodes. Once a Host is bound, the normal guarded retirement applies.
        """
        ...

    async def set_rotation_hold(
        self,
        *,
        host: str,
        allocation_id: str,
        source_id: str,
        scope: str | None,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Pause, or with no scope resume, claims on a Host while it changes machine.

        Keyed by the Host name: the hold moves with the Host between allocations.
        """
        ...

    async def rebind_slot_host(
        self,
        *,
        host: str,
        from_node_id: str,
        to_node_id: str,
        operation_id: str,
        generation: int,
        worker_id: str,
    ) -> None:
        """Hand a Host name from one node to the node that now holds its state.

        The one deliberate exception to "a Host name never changes node": the
        caller copied the Host's complete container state to the target and
        keeps claims paused. Only a rotation's own successor (or, to roll back,
        its predecessor) may take the name. The previous node's lease, identity
        and bootstrap holds stop applying; the rotation hold follows the Host.
        """
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
        """Cancel a safe retirement or rotation intent and remove only its own restriction.

        A rotation can be cancelled until the old node stops: its claim pause
        is lifted and its replacement node, if any, is queued for release.
        """
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
