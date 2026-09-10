"""Sandbox activation, admission, settlement and reconciliation.

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol


class SandboxRepository(Protocol):
    """Sandbox activation, admission, settlement and reconciliation."""

    async def thread_workspace_ready(
        *,
        thread_id: str,
    ) -> bool:
        """Return whether a retained thread permits owner workspace mutation."""
        ...

    async def fence_terminal_workspace(
        *,
        attempt_id: int,
        lease_generation: int,
    ) -> bool:
        """Revoke readiness only for the still-live fenced attempt."""
        ...

    async def list_terminal_resumes_pending(
        *,
        limit: int = 100,
    ) -> list[str]:
        """List settled workspaces whose broker resume still needs confirmation."""
        ...

    async def mark_terminal_resume_complete(
        *,
        job_id: str,
    ) -> bool:
        """Clear durable recovery state after the broker confirms a resume."""
        ...

    async def reserve_thread_sandbox(
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        host: str,
        sandbox_id: str,
        container_name: str,
        runtime_id: str,
        runtime_version: str | None,
        sandbox_image_digest: str | None,
        execution_profile: str,
        resident_slot_capacity: int = 8,
        memory_limit: str = "4g",
        cpu_limit: str = "2",
        stop_timeout_seconds: int = 10,
    ) -> dict[str, Any] | None:
        """Reserve/start one thread's durable sandbox under its attempt fence."""
        ...

    async def mark_thread_sandbox_running(
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        container_id: str,
        context_status: str,
    ) -> dict[str, Any] | None:
        """Confirm Docker start under both attempt and sandbox generations."""
        ...

    async def record_thread_sandbox_process(
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        process_generation: int,
    ) -> bool:
        """Record the supervisor's diagnostic CLI process generation."""
        ...

    async def record_shared_thread_residency(
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_id: str,
        residency_generation: int,
    ) -> bool:
        """Bind a residency and its immutable attribution before CLI startup."""
        ...

    async def admit_thread_sandbox_turn(
        *,
        thread_id: str,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
    ) -> bool:
        """Reserve one prompt delivery durably before Agent CLI stdin."""
        ...

    async def settle_thread_sandbox(
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        job_id: str,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Close one turn boundary and arm deactivation without deleting state."""
        ...

    async def reconcile_thread_sandbox_observation(
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
        running: bool,
        activity_active: bool | None = None,
        idle_remaining_seconds: float | None = None,
        members: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Reconcile one authenticated shared-group Docker observation."""
        ...

    async def due_thread_sandbox_stop(
        *,
        thread_id: str,
        host: str,
        sandbox_id: str,
        container_id: str,
        sandbox_generation: int,
    ) -> dict[str, Any] | None:
        """Return the exact shared-group fence for a due retained stop."""
        ...

    async def abandon_thread_sandbox(
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        lost: bool,
        detail: str | None,
        idle_timeout_seconds: float,
    ) -> dict[str, Any] | None:
        """Release a failed shared-member activation without replaying it."""
        ...

    async def thread_sandbox(
        *,
        thread_id: str,
    ) -> dict[str, Any] | None:
        """Return the durable local sandbox mapping, including STOPPED/LOST."""
        ...
