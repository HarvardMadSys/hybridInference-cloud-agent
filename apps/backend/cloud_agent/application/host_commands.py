"""Host administration shared by HTTP handlers and resource orchestration.

These commands own execution-pool decisions only. Provider lifecycle changes
and cloud credentials never enter this module. HTTP authentication, audit and
snapshot formatting remain with the existing administrative routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_agent.host_pool import hash_secret, new_enroll_token
from cloud_agent_shared.runtime_catalog import downloadable_runtimes

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from cloud_agent.ports.host_pool import HostPoolRepository


class HostCommandError(Exception):
    """A public administrative refusal with the existing response semantics."""

    def __init__(self, status: int, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class SandboxPlacement:
    """One global destination for new sandbox groups; existing owners stay fixed."""

    def __init__(self, repository: HostPoolRepository) -> None:
        self.repository = repository

    async def set_active(self, host: str | None) -> str | None:
        """Validate and change placement atomically through the pool contract."""
        previous = await self.repository.active_runner_host()
        if not await self.repository.set_active_runner_host(host=host):
            current = await self.repository.get_host(host=host) if host else None
            if current is not None:
                raise HostCommandError(
                    409,
                    f"{host!r} cannot accept new sandboxes while its compute resource is unavailable, expiring, or retiring.",
                )
            raise HostCommandError(
                404,
                f"No runner on {host!r} has ever polled. Start one there "
                "(deploy/host/sandbox_host.sh up) and it will appear here.",
            )
        return previous


class HostCommands:
    """Run existing Host use cases independently of an HTTP request."""

    def __init__(self, repository: HostPoolRepository) -> None:
        self.repository = repository

    async def enroll_token(
        self, *, host: str, ttl_seconds: float, actor: str | None
    ) -> tuple[str, dict[str, Any]]:
        """Issue a Host-bound token; only the caller receives its plaintext."""
        token = new_enroll_token()
        issued = await self.repository.issue_enroll_token(
            token_hash=hash_secret(token), host=host, ttl_seconds=ttl_seconds, issued_by=actor
        )
        return token, issued

    async def runtime_version(self, *, runtime: str, version: str | None) -> dict[str, str]:
        """Select only an installed runtime and return previous selections for audit."""
        _, _, previous, _ = await self.repository.runner_pool()
        if not await self.repository.set_runtime_version(runtime=runtime, version=version):
            if runtime in downloadable_runtimes():
                how = (
                    "Update to it from Settings → Agent versions: every host downloads "
                    "and checks it, then new chats switch to it."
                )
            else:
                how = (
                    "A version has to be built into the sandbox image before it can be "
                    "selected — add it to that host's *_EXTRA_VERSIONS and redeploy."
                )
            raise HostCommandError(404, f"No host in the pool offers {runtime} {version!r}. {how}")
        return previous

    async def forget(self, host: str) -> None:
        """Forget administrative history only when no retained dependencies remain."""
        outcome = await self.repository.forget_runner_host(host=host)
        if outcome == "active":
            raise HostCommandError(
                409,
                f"{host!r} is the active host. Switch to another host (or unpin) before removing it.",
            )
        if outcome == "fenced":
            raise HostCommandError(
                409,
                f"{host!r} is held by an automated deployment drain and cannot be forgotten.",
            )
        if outcome == "unknown":
            raise HostCommandError(404, f"Unknown runner host: {host}")
        if outcome != "deleted":
            raise HostCommandError(
                409,
                f"{host!r} still has {outcome.replace('_', ' ')}; clear those dependencies before forgetting it.",
            )

    async def set_status(self, *, host: str, status: str, force: bool = False) -> None:
        """Apply the existing drain/undrain transition table."""
        outcome = await self.repository.set_host_status(host=host, status=status, force=force)
        if outcome == "unknown":
            raise HostCommandError(404, f"Unknown runner host: {host}")
        if outcome == "fenced":
            raise HostCommandError(
                409,
                f"{host!r} is held by an automated deployment drain; use its fenced release or repeat with force=true.",
            )
        if outcome == "invalid":
            current = await self.repository.get_host(host=host)
            raise HostCommandError(
                409,
                f"{host!r} is {current['status'] if current else 'unknown'!r} and cannot become {status!r}.",
            )

    async def acquire_drain_fence(
        self,
        *,
        host: str,
        expected_fence_token: UUID | None,
        expected_fence_epoch: UUID,
        fence_token: UUID,
    ) -> tuple[str, UUID]:
        """Acquire or idempotently confirm one deployment-owned drain."""
        outcome, status_before, drain_fence_epoch = await self.repository.acquire_host_drain_fence(
            host=host,
            expected_fence_token=expected_fence_token,
            expected_fence_epoch=expected_fence_epoch,
            fence_token=fence_token,
        )
        if outcome == "unknown":
            raise HostCommandError(404, f"Unknown runner host: {host}")
        if outcome == "manual":
            raise HostCommandError(
                409,
                f"{host!r} is draining without an automated owner and cannot be acquired.",
            )
        if outcome in ("stale", "released"):
            raise HostCommandError(409, f"The deployment drain ownership for {host!r} changed.")
        if outcome == "invalid":
            current = await self.repository.get_host(host=host)
            raise HostCommandError(
                409,
                f"{host!r} is {current['status'] if current else 'unknown'!r} and cannot be fenced.",
            )
        if status_before not in ("online", "draining"):
            raise HostCommandError(409, f"The deployment drain receipt for {host!r} is invalid.")
        if drain_fence_epoch is None:
            raise HostCommandError(409, f"The deployment drain epoch for {host!r} is invalid.")
        return status_before, drain_fence_epoch

    async def release_drain_fence(self, *, host: str, fence_token: UUID) -> UUID:
        """Release or idempotently confirm the exact deployment-owned drain."""
        outcome, drain_fence_epoch = await self.repository.release_host_drain_fence(
            host=host, fence_token=fence_token
        )
        if outcome == "unknown":
            raise HostCommandError(404, f"Unknown runner host: {host}")
        if outcome == "stale":
            raise HostCommandError(409, f"The deployment drain ownership for {host!r} changed.")
        if outcome == "invalid":
            current = await self.repository.get_host(host=host)
            raise HostCommandError(
                409,
                f"{host!r} is {current['status'] if current else 'unknown'!r} and cannot be released.",
            )
        if drain_fence_epoch is None:
            raise HostCommandError(409, f"The deployment drain epoch for {host!r} is invalid.")
        return drain_fence_epoch

    async def remove(self, *, host: str, force: bool = False, allow_unmoved: bool = False) -> bool:
        """Revoke Host access without releasing any underlying machine.

        Returns whether the removal was deferred: the Host stops taking work
        now, moves its sandboxes to other Hosts when they can move, and is
        revoked once they have and its running attempts end.
        """
        outcome = await self.repository.remove_host(
            host=host, force=force, allow_unmoved=allow_unmoved
        )
        if outcome == "unknown":
            raise HostCommandError(404, f"Unknown runner host: {host}")
        if outcome == "no_move_target":
            raise HostCommandError(
                409,
                f"No other server can take the sandboxes on {host!r} now, or checkpoints are "
                "off. Removing it anyway leaves them on it, and their chats cannot continue "
                "until it returns. Repeat with allow_unmoved=true to remove it anyway.",
                code="no_move_target",
            )
        if outcome == "cannot_move":
            raise HostCommandError(
                409,
                f"Some sandboxes on {host!r} have no checkpoint another server can restore, "
                "for example because a checkpoint failed, so they cannot move. Removing it "
                "leaves them on it, and their chats cannot continue until it returns. Repeat "
                "with allow_unmoved=true to remove it anyway; the other sandboxes still move "
                "first.",
                code="cannot_move",
            )
        if outcome == "busy":
            raise HostCommandError(
                409,
                f"{host!r} is still running attempts and cannot stop taking work. Repeat with "
                "force=true to expire their leases now under the retained-context fail-closed policy.",
            )
        if outcome == "fenced":
            raise HostCommandError(
                409,
                f"{host!r} is held by an automated deployment drain; release it first or repeat with force=true.",
            )
        return outcome in {"draining", "moving_out"}

    async def global_settings(self, changes: Mapping[str, int | None]) -> dict[str, Any]:
        """Change runtime settings for every server; return the old values for audit."""
        return await self.repository.set_host_settings(changes=changes)

    async def settings(self, *, host: str, changes: Mapping[str, int | None]) -> dict[str, Any]:
        """Change one server's runtime settings; return its old values for audit."""
        previous = await self.repository.set_runner_host_settings(host=host, changes=changes)
        if previous is None:
            raise HostCommandError(404, f"Unknown runner host: {host}")
        return previous

    async def slots(self, *, host: str, slots: int | None) -> dict[str, Any]:
        """Save desired capacity and return the old observation for audit."""
        before = await self.repository.get_host(host=host)
        if before is None:
            raise HostCommandError(404, f"Unknown runner host: {host}")
        outcome = await self.repository.set_host_slots_desired(host=host, slots=slots)
        if outcome == "unknown":
            raise HostCommandError(404, f"Unknown runner host: {host}")
        if outcome == "autoscaling_managed":
            raise HostCommandError(
                409,
                f"{host!r} is managed by CloudLab autoscaling; its slot target is derived "
                "from fresh capacity telemetry and cannot be edited directly.",
            )
        return before
