"""Port: retained sandbox groups and the members that live in them.

Reservation, admission, settlement, and the fencing a terminal workspace
needs. The most stateful surface in the control plane.

Declared here, beside the code that calls it, so a caller depends on the
operations it needs rather than on the whole persistence surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from datetime import datetime


class SandboxRepository(Protocol):
    """Port: retained sandbox groups and the members that live in them."""

    async def request_admin_delete(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str,
        last_activity_at: datetime,
        actor_id: str,
        admin_ip: str,
        sandbox_manager_confirmed_stopped: bool = False,
        confirmed_docker_status: str | None = None,
        discard: bool = False,
        source: str = "admin",
    ) -> str:
        """Fence an idle stopped sandbox for deletion.

        A durable request closes admission before the host removes Docker. The
        default path exports and verifies an S3 archive first. ``discard``
        removes the exact stopped or dead container without uploading it. No native
        state or ledger row is removed until the host confirms that removal.
        An admin recovery may also repair a stale RUNNING row, but only after
        the API independently proves the exact host container is stopped or dead, and idle.
        ``source`` is ``system`` when it removes the sandbox of deleted chats.
        """
        ...

    async def claim_due_admin_sandbox_deletion(
        self, *, ttl_seconds: float
    ) -> dict[str, Any] | None:
        """Lease one pending or archive-verified deletion without duplicate workers."""
        ...

    async def record_admin_sandbox_archive(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        archive_key: str,
        sha256: str,
        size_bytes: int,
        archive_version_id: str | None = None,
    ) -> bool:
        """Checkpoint an independently verified S3 receipt under the live worker lease."""
        ...

    async def complete_admin_sandbox_deletion(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
    ) -> bool:
        """Retain the archive and ledger after an exact host deletion acknowledgement."""
        ...

    async def complete_admin_sandbox_discard(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        reclaimed_quiet_seconds: float | None = None,
    ) -> bool:
        """Mark native context lost after the host removes a container with no archive.

        With ``reclaimed_quiet_seconds``, finish without the host instead, only
        when its provider confirmed the machine terminated and the host has
        been silent that long; otherwise return ``False`` and change nothing.
        """
        ...

    async def fail_admin_sandbox_deletion(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        error: str,
        retain_pending: bool = False,
    ) -> bool:
        """Release safe pre-archive failures but keep ambiguous deletion fenced."""
        ...

    async def renew_admin_sandbox_deletion_claim(
        self,
        *,
        sandbox_id: str,
        request_id: str,
        claim_token: str,
        generation: int,
        container_id: str,
        ttl_seconds: float = 300,
        for_cleanup: bool = False,
        cleanup_archive_key: str | None = None,
    ) -> bool:
        """Extend an unexpired exact worker claim before bounded host cleanup."""
        ...

    async def set_admin_archived(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str | None,
        last_activity_at: datetime,
        archived: bool,
        actor_id: str,
        admin_ip: str,
        allow_stale_host: bool = False,
    ) -> str:
        """Audit a reviewed inventory removal or restore without deleting state.

        Stopped, quiescent groups may be hidden. An explicit stale-host
        removal also permits quiescent groups whose host has no fresh
        heartbeat, without changing their lifecycle or native state. A new
        turn or running host observation makes a removed group visible again
        automatically. Containers, native sessions and ledger rows survive.
        """
        ...

    async def record_terminal_observation(
        self,
        *,
        sandbox_id: str,
        host: str,
        generation: int,
        container_id: str,
        terminal_count: int,
    ) -> bool:
        """Refresh terminal activity only for the exact running container."""
        ...

    async def request_admin_stop(
        self,
        *,
        sandbox_id: str,
        generation: int,
        container_id: str,
        last_activity_at: datetime,
        actor_id: str,
        admin_ip: str,
    ) -> str:
        """Queue a reviewed, quiescent container stop under its admission fence.

        The host still checks its local terminals and operations before acting.
        The database marker survives observations and excludes new admission
        until Docker confirms the stop. No container or workspace is deleted.
        """
        ...

    async def sandboxes_of_deleted_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """List live sandboxes on online hosts whose every chat was deleted."""
        ...

    async def request_deleted_sessions_stop(
        self, *, sandbox_id: str, generation: int, container_id: str, actor_id: str
    ) -> str:
        """Ask the host to stop a running sandbox whose every chat was deleted."""
        ...

    async def sandboxes_moving_out(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """List running groups on Hosts moving out, oldest activity first."""
        ...

    async def request_move_out_stop(
        self, *, sandbox_id: str, generation: int, container_id: str, actor_id: str
    ) -> str:
        """Ask a Host moving out to stop one idle group so its checkpoint can move it."""
        ...

    async def release_moving_out_groups(self) -> list[str]:
        """Hand every covered stopped group on a Host moving out to another Host."""
        ...

    async def thread_workspace_ready(self, *, thread_id: str) -> bool:
        """Return whether a retained thread permits owner workspace mutation.

        The sandbox manager's in-memory suspension is the immediate barrier. This
        durable copy closes sandbox-manager-restart and old-turn-URL gaps: an active
        attempt must have published ``workspace_ready``.
        """
        ...

    async def fence_terminal_workspace(self, *, attempt_id: int, lease_generation: int) -> bool:
        """Revoke readiness only for the still-live fenced attempt."""
        ...

    async def list_terminal_resumes_pending(self, *, limit: int = 100) -> list[str]:
        """List settled workspaces whose sandbox manager resume still needs confirmation."""
        ...

    async def mark_terminal_resume_complete(self, *, job_id: str) -> bool:
        """Clear durable recovery state after the sandbox manager confirms a resume."""
        ...

    async def reserve_thread_sandbox(
        self,
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
        disk_limit_mib: int = 0,
        stop_timeout_seconds: int = 10,
        fork_helper_version: str | None = None,
    ) -> dict[str, Any] | None:
        """Reserve/start one thread's durable sandbox under its attempt fence.

        Unlike HOT residency this mapping is never taken over by another host:
        before persistent storage, the retained container's writable layer is
        the only copy. A different host must leave the turn queued rather than
        create an empty replacement and call it continuity.

        ``fork_helper_version`` is the image's ``claude-session-fork`` version;
        it is recorded with a new group and ignored for one that already exists.
        """
        ...

    async def mark_thread_sandbox_running(
        self,
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
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
        process_generation: int,
    ) -> bool:
        """Record an equal or newer supervisor CLI process generation."""
        ...

    async def record_shared_thread_residency(
        self,
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
        self,
        *,
        thread_id: str,
        job_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
    ) -> bool:
        """Reserve one prompt delivery durably before Agent CLI stdin.

        The update is idempotent for the same job/attempt, but a different job
        cannot replace an unresolved admission.  A control-plane reservation
        followed by a runner crash is intentionally conservative: the reaper
        records LOST instead of sending the same user message a second time.
        """
        ...

    async def settle_thread_sandbox(
        self,
        *,
        thread_id: str,
        attempt_id: int,
        lease_generation: int,
        sandbox_generation: int,
        activation_generation: int,
        residency_generation: int,
        job_id: str,
        context_status: str,
        native_session_id: str | None,
        idle_timeout_seconds: float,
        base_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Close one turn boundary and arm deactivation without deleting state."""
        ...

    async def reconcile_thread_sandbox_observation(
        self,
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
        resource_usage: dict[str, Any] | None = None,
        terminal_count: int | None = None,
    ) -> str | None:
        """Reconcile one authenticated shared-group Docker observation."""
        ...

    async def record_sandbox_checkpoint(
        self,
        *,
        host: str,
        thread_id: str,
        checkpoint: dict[str, Any],
    ) -> bool:
        """Record one host-reported checkpoint receipt for its current group."""
        ...

    async def fence_workspace_start(self, *, thread_id: str) -> tuple[str, int | None] | None:
        """Count a start a workspace request may cause; return state and fence.

        The fence is the ``start_seq`` the request must carry. ``host_revoked``
        with no fence when the group's host has left the pool: nothing may
        start the group there, and it is not fenced.
        """
        ...

    async def due_thread_sandbox_stop(
        self,
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
        self,
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

    async def thread_sandbox(self, *, thread_id: str) -> dict[str, Any] | None:
        """Return the durable local sandbox mapping, including STOPPED/LOST."""
        ...
