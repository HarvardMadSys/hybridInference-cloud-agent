"""Owner environment persistence and frozen execution bindings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import builtins

    from cloud_agent_shared.environment import BoundEnvironment


class EnvironmentRepository(Protocol):
    """The configuration and execution queries exposed to application flows."""

    async def list_environments(
        self, user_id: str, repo: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """List active owned environments with default and test facts."""
        ...

    async def get_environment(self, user_id: str, environment_id: str) -> dict[str, Any]:
        """Read an owned environment without exposing secret values."""
        ...

    async def get_global_environment(self, user_id: str) -> dict[str, Any]:
        """Read personal global variables or the empty initial configuration."""
        ...

    async def save_environment(
        self,
        user_id: str,
        *,
        repo: str | None,
        name: str,
        environment_id: str | None,
        revision: int,
        document: dict[str, Any],
        edits: builtins.list[dict[str, Any]],
        expected_global_revision: int | None = None,
    ) -> dict[str, Any]:
        """Publish an immutable version under an owner-wide transaction lock."""
        ...

    async def archive_environment(self, user_id: str, environment_id: str, revision: int) -> None:
        """Hide a configuration while retaining referenced encrypted versions."""
        ...

    async def set_environment_default(
        self, user_id: str, repo: str, environment_id: str | None, revision: int
    ) -> dict[str, Any]:
        """Set or clear the repository default with compare-and-swap."""
        ...

    async def list_environment_versions(
        self, user_id: str, environment_id: str, before: int | None = None
    ) -> builtins.list[dict[str, Any]]:
        """Return bounded history without secret values."""
        ...

    async def copy_environment(
        self,
        user_id: str,
        environment_id: str,
        *,
        repo: str,
        name: str,
        revision: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Copy a current owned configuration to an independent identity."""
        ...

    async def restore_environment(
        self, user_id: str, environment_id: str, *, revision: int, source_revision: int
    ) -> dict[str, Any]:
        """Create a new version from owned immutable history."""
        ...

    async def get_environment_binding(self, user_id: str, binding_id: str) -> BoundEnvironment:
        """Decrypt only a fixed authorized execution snapshot."""
        ...

    async def refresh_runtime_environment(
        self, user_id: str, binding_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Explicitly update runtime variables while preserving installation."""
        ...

    async def request_skip_environment_start(
        self, user_id: str, binding_id: str, job_id: str
    ) -> None:
        """Arm one user turn after a failed startup."""
        ...

    async def environment_for_claim(
        self, user_id: str, binding_id: str, job_id: str
    ) -> BoundEnvironment:
        """Read execution variables and consume explicit one-turn recovery."""
        ...

    async def get_job_environment(self, job_id: str) -> BoundEnvironment | None:
        """Resolve immutable owned configuration for trusted publication paths."""
        ...

    async def workspace_environment_view(self, user_id: str, thread_id: str) -> dict[str, Any]:
        """Read bound configuration and preparation facts for an owned thread."""
        ...

    async def find_environment_run(
        self, user_id: str, key: str, selection: dict[str, Any], ref: str | None
    ) -> dict[str, Any] | None:
        """Return the original logical test request before resolving a branch."""
        ...

    async def fail_environment_preflight(self, run_id: str, host: str, generation: int) -> bool:
        """Release a fenced test claim before any host execution starts."""
        ...

    async def redact_environment_steps(
        self, user_id: str, binding_id: str, steps: builtins.list[dict[str, Any]]
    ) -> builtins.list[dict[str, Any]]:
        """Mask read-only host output using the exact immutable binding."""
        ...

    async def create_environment_run(
        self,
        user_id: str,
        *,
        repo: str,
        base_sha: str,
        selection: dict[str, Any],
        key: str | None,
        ref: str | None = None,
    ) -> dict[str, Any]:
        """Queue an isolated environment test with an immutable binding."""
        ...

    async def get_environment_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        """Read an owned test run and sanitized output."""
        ...

    async def cancel_environment_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        """Fence cancellation and release queued or running tests."""
        ...

    async def list_environment_orphans(self, host: str) -> builtins.list[dict[str, Any]]:
        """Return this host's fenced tests awaiting exact cleanup."""
        ...

    async def maintain_environments(self) -> None:
        """Expire tests and collect only released archived references."""
        ...

    async def claim_environment_run(
        self, host: str, credential_hash: str, attestation: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Claim a queued test or reap expired execution leases."""
        ...

    async def update_environment_run(
        self, run_id: str, host: str, generation: int, update: dict[str, Any]
    ) -> bool:
        """Heartbeat or finish only the live matching test execution."""
        ...

    async def create_environment_action_run(
        self, user_id: str, thread_id: str, binding_id: str, action_id: str, key: str
    ) -> dict[str, Any]:
        """Reserve an idempotent owner operation in a retained workspace."""
        ...

    async def get_environment_action_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        """Read one owned manual operation."""
        ...

    async def update_environment_action_run(
        self, user_id: str, run_id: str, update: dict[str, Any]
    ) -> dict[str, Any]:
        """Record terminal identity and masked terminal status."""
        ...

    async def record_environment_preparation(
        self, user_id: str, thread_id: str, receipt: dict[str, Any]
    ) -> None:
        """Persist host receipts against the matching owned group binding."""
        ...

    async def record_worker_environment_preparation(
        self, job_id: str, attempt_id: int, generation: int, receipt: dict[str, Any]
    ) -> bool:
        """Persist a lifecycle receipt only under the matching live attempt fence."""
        ...

    async def save_environment_preparation(
        self,
        sandbox_id: str,
        binding_id: str,
        container_id: str,
        boot_id: str,
        steps: builtins.list[dict[str, Any]],
    ) -> None:
        """Persist preparation results using immutable binding and container identity."""
        ...
