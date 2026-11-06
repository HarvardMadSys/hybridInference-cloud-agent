"""What the runner tells the sandbox manager, and what it reads back: settings and health."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from cloud_agent_host import sandbox_manager_profiling_policy
from cloud_agent_host.archive.sandbox_archive_s3 import SandboxArchiveConfigError
from cloud_agent_host.manager_api.routing import route
from cloud_agent_host.terminal_session import _TerminalLimits
from cloud_agent_shared.environment import ENVIRONMENT_CAPABILITY, ENVIRONMENT_INFERENCE_CAPABILITY
from cloud_agent_shared.environment_snapshots import ENVIRONMENT_SNAPSHOT_CAPABILITY
from cloud_agent_shared.host_settings import (
    SANDBOX_MANAGER_SETTING_KEYS,
    admin_values,
    default_values,
    resolve,
)
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicyError
from cloud_agent_shared.ssh_access import SSH_ACCESS_CAPABILITY

if TYPE_CHECKING:
    from starlette.datastructures import State

    from cloud_agent_host.manager_api.context import ManagerContext

_DEFAULT_SETTINGS = default_values(SANDBOX_MANAGER_SETTING_KEYS)


class SettingsRoutes:
    """The runner's configuration pushes and its health probe."""

    def __init__(self, context: ManagerContext, state: State) -> None:
        self._context = context
        self._state = state

    @route("POST", "/profiling-policy")
    def profiling_policy(self, body: dict[str, Any]) -> dict[str, Any]:
        """Accept the profiling policy this host's runner pushes.

        Reached over the sandbox manager's own shared-token channel and nowhere else.
        The sandbox manager holds no host credential and must not be given one -- the
        design keeps that out of the sandbox side entirely -- so the runner,
        which does hold one, is what tells it what the deployment is collecting
        for. Applying the policy through the same store the collector reads is
        what makes a fragment possible at all: a sandbox manager with no policy refuses
        every selection, however well-formed the header that carried it.
        """
        return sandbox_manager_profiling_policy.apply_received_policy(body)

    @route("POST", "/host-settings")
    def host_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply the complete admin configuration, with defaults for missing keys.

        The runner sends them after every heartbeat, over the same
        shared-token channel the profiling policy uses: the sandbox manager holds no
        host credential and must not be given one. The whole set every time,
        so a cleared or unsupported value uses the shared built-in default.
        Legacy environment values cannot override a reset from the admin page.

        Answers with what it now applies and what its own would be, which the
        runner reports to the control plane as this host's confirmation.
        """
        settings = self._context.settings
        admin = {
            **_DEFAULT_SETTINGS,
            **admin_values(body.get("settings"), SANDBOX_MANAGER_SETTING_KEYS),
        }
        applied = resolve(admin, settings.local)
        self._context.terminals.configure(
            idle_ttl_s=float(applied["terminal_idle_ttl_s"]),
            hard_ttl_s=float(applied["terminal_hard_ttl_s"]),
            max_sessions=int(applied["terminal_max_sessions"]),
            max_sessions_per_workspace=int(applied["terminal_max_sessions_per_workspace"]),
            limits=_TerminalLimits.from_settings(applied),
        )
        self._context.supervisor.set_idle_timeout_s(float(applied["chat_idle_timeout_s"]))
        settings.applied = applied
        return {"applied": applied, "local": settings.local}

    @route("POST", "/sandbox-archive-settings")
    async def sandbox_archive_settings(self, body: dict[str, Any]) -> dict[str, object]:
        """Apply one complete DB policy and confirm its exact revision.

        This authenticated runner-to-sandbox-manager channel may carry one saved AWS
        access key. The sandbox manager keeps it in memory for S3 and never writes it
        into a sandbox marker. With no key, the sandbox manager's workload identity
        remains the S3 authority. The policy names the bucket; the prefix stays fixed.
        """
        controller = self._context.archive_controller
        if controller is None:
            raise HTTPException(
                status_code=409,
                detail="sandbox manager does not use dynamic sandbox archive settings",
            )
        if set(body) != {"settings"} or not isinstance(body.get("settings"), dict):
            raise HTTPException(
                status_code=422,
                detail="sandbox archive settings must be one complete object",
            )

        def apply_and_start() -> dict[str, object]:
            status = controller.apply(body["settings"])
            # Apply first: reconciliation may immediately stop and archive an
            # orphaned retained container through this controller.
            self._context.supervisor_start()
            return status

        try:
            return await self._context.endpoint_calls.run(apply_and_start)
        except (SandboxArchivePolicyError, SandboxArchiveConfigError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @route("GET", "/health")
    def health(self) -> dict[str, Any]:
        """Name this sandbox manager's capabilities and its archive's state."""
        controller = self._context.archive_controller
        return {
            "ok": True,
            "capabilities": [
                ENVIRONMENT_CAPABILITY,
                ENVIRONMENT_INFERENCE_CAPABILITY,
                SSH_ACCESS_CAPABILITY,
                *(
                    [ENVIRONMENT_SNAPSHOT_CAPABILITY]
                    if controller is not None and controller.snapshots_available
                    else []
                ),
            ],
            "sandbox_archives": self._archive_health(),
            "retained_docker_data_root": getattr(
                self._state,
                "retained_docker_data_root",
                None,
            ),
        }

    def _archive_health(self) -> dict[str, Any]:
        context = self._context
        if context.archive_service is not None:
            return {
                **context.archive_service.status(),
                "shutdown_candidates": (context.supervisor.shutdown_archive_candidate_count()),
            }
        if context.archive_controller is None:
            return {"enabled": False}
        return {
            **context.archive_controller.status(),
            "supervisor_ready": context.supervisor_start.ready,
            "shutdown_candidates": (
                context.supervisor.shutdown_archive_candidate_count()
                if context.supervisor_start.ready
                else 0
            ),
        }


__all__ = ["SettingsRoutes"]
