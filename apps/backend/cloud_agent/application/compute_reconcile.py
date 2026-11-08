"""Durable acquisition, deployment, renewal, and retirement steps.

Every side effect has a preceding checkpoint. A recovered acquisition is
queried, never blindly recreated. The execution lease fences database writes;
fixed resource identity and request parameters bound the external effects.

The reconciler schedules the periodic work, claims and dispatches each
operation, and holds the step primitives every workflow shares (checkpoint,
finish, fence, observation). Renewal and Host rollout run here; acquisition
(:mod:`~cloud_agent.application.compute_acquisition`), retirement
(:mod:`~cloud_agent.application.compute_retirement`), the provider release
(:mod:`~cloud_agent.application.compute_release`) and node rotation
(:mod:`~cloud_agent.application.compute_rotation`) each run in their own
collaborator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from cloud_agent.application.compute_acquisition import ComputeAcquisition
from cloud_agent.application.compute_diagnostic_values import (
    cloudlab_observation_diagnostics,
    provider_diagnostic_result,
)
from cloud_agent.application.compute_release import EXPIRED_NOT_FOUND, ComputeRelease
from cloud_agent.application.compute_retirement import ComputeRetirement
from cloud_agent.application.compute_rotation import (
    ComputeRotation,
    host_release_blocks_rotation,
    load_host_release_for_allocations,
)
from cloud_agent.application.compute_setup import HostRelease, load_control_plane_host_release
from cloud_agent.domain.compute import (
    MOVE_OUT_RENEWAL_LEAD_SECONDS,
    RECLAIMED_QUIET_SECONDS,
    ComputeRefusal,
    fingerprint,
    move_out_window_seconds,
)
from cloud_agent.logging_utils import get_logger
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.ports.external.compute_provider import (
    AcquireRequest,
    ExtendRequest,
    NodeObservation,
    OperationIntent,
    ProviderError,
    ResourceObservation,
    ResourceRef,
)
from cloud_agent.ports.external.host_bootstrap import (
    BootstrapIntent,
    BootstrapResult,
    DeploymentProfile,
)
from cloud_agent.providers.compute import compute_config
from cloud_agent.providers.compute.compute_secrets import ComputeSecretError
from cloud_agent.sandbox_manager.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager.sandbox_manager_routing import sandbox_manager_for_host
from cloud_agent.schemas.compute import ExtendComputeRequest, RenewalPolicy
from cloud_agent_shared.shared_protocol import shared_deployment_capability

if TYPE_CHECKING:
    from cloud_agent.application.compute_resources import ComputeCommands
    from cloud_agent.ports.external.host_bootstrap import HostBootstrap
    from cloud_agent.ports.host_pool import HostPoolRepository

logger = get_logger(__name__)

_BOOTSTRAP_REASONS = {
    "supported_os": "Use Ubuntu 22.04 or 24.04.",
    "architecture": "The deployment requires x86-64.",
    "privileged_deploy": "SSH deployment requires passwordless sudo.",
    "kvm": "KVM is required for sandbox isolation.",
    "docker": "Install Docker or enable prerequisite installation.",
    "compose": "Docker Compose is unavailable.",
    "data_mount_unavailable": "Sandbox storage must be an existing mounted data disk.",
    "data_mount_conflict": "Sandbox storage conflicts with an existing mount or retained daemon data.",
    "data_mount_failed": "The selected sandbox storage could not be prepared.",
    "machine_identity_changed": "The verified machine identity has changed.",
    "deployment_identity_conflict": (
        "The node already holds a different deployment. Inspect the node; a retry will not replace it."
    ),
    "unsafe_deployment_path": (
        "The deployment directory on the node is a link or unsafe path. Repair it before retrying."
    ),
    "invalid_deployment_archive": (
        "The deployment bundle contains an unsafe entry. Fix the deployment profile before retrying."
    ),
    "prerequisites_update_failed": "The node could not refresh its package repositories.",
    "prerequisites_install_failed": "The node could not install the required packages.",
    "docker_start_failed": "Docker could not be started.",
    "kata_install_failed": "The pinned Kata runtime could not be installed.",
    "host_deployment_failed": "The Host deployment recipe failed.",
    "host_readiness_failed": "The deployed Host did not pass its readiness checks.",
}
# A host release must not run beside another deployment of the same machine.
_RELEASE_MUTATIONS = {"acquire", "import", "upgrade", "retire", "rotate", "discard"}
_RELEASE_ACTIVE = {"pending", "running", "waiting", "unknown"}
# Prepare can fail without draining. Stop retrying a release that keeps failing
# that way; an administrator retry clears the counter and tries again.
_RELEASE_ATTEMPTS = 3
# A deployment drain fence normally lasts one rollout. Kept this long on a Host
# that is off the control plane's commit, it is reported: the Host can neither
# take work nor be rolled forward until someone resumes it.
_RELEASE_FENCE_ALERT_AFTER = timedelta(minutes=30)


# How long past its recorded deadline a missing resource is taken as ended.
_EXPIRY_GRACE = timedelta(minutes=10)
# How long an extension the provider neither applies nor refuses is awaited.
# Every other operation of the allocation waits for it meanwhile; afterwards
# it blocks, so a move can start and a retry can send the same target again.
_EXTENSION_CONFIRMATION = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(UTC)


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


class ComputeReconciler:
    """Explicit workflows over narrow ports, without a generic workflow engine."""

    def __init__(
        self, *, commands: ComputeCommands, host_pool: HostPoolRepository, bootstrap: HostBootstrap
    ) -> None:
        self.commands = commands
        self.repository = commands.repository
        self.host_pool = host_pool
        self.bootstrap = bootstrap
        self.rotation = ComputeRotation(self)
        self.release = ComputeRelease(self)
        self.acquisition = ComputeAcquisition(self)
        self.retirement = ComputeRetirement(self)
        self._move_out_reminded: set[str] = set()
        self._move_out_blocked_reported: set[str] = set()
        self._release_fenced_since: dict[str, datetime] = {}
        self._release_fence_reported: set[str] = set()

    async def schedule(self) -> None:
        """Observe leases and submit renewals even while retirement is blocked.

        A released resource not yet seen gone is still read, and only read.
        """
        now = _now()
        observation_seconds = compute_config.current_observation_seconds(
            self.commands.configuration
        )
        snapshot = await self.repository.scheduling_inventory()
        release = await load_host_release_for_allocations(
            snapshot.allocations, load_control_plane_host_release
        )
        for row in snapshot.allocations:
            if row.get("observed_state") == "terminated":
                await self._lose_reclaimed_groups(row)
            if row.get("observed_state") in {"terminated", "absent"}:
                continue
            operations = snapshot.operations.get(row["id"], [])
            if row.get("external_id"):
                await self._queue_due_inspection(row, operations, now, observation_seconds)
            if row.get("release_committed_at"):
                continue
            if release is not None and row.get("desired_state") == "present":
                await self._queue_host_release(row, operations, release, now)
            policy = RenewalPolicy.model_validate(row.get("policy") or {})
            expires = _moment(row.get("expires_at"))
            if expires is not None and row.get("expiration_known"):
                await self._report_blocked_move_out(row, policy, now, expires)
                await self._remind_unfinished_move_out(row, policy, now, expires)
            if not policy.enabled or expires is None or not row.get("expiration_known"):
                continue
            # An extension is tried, and granted or given up, before the
            # move-out window opens, so a granted one keeps the groups where
            # they are.
            window = move_out_window_seconds(policy.model_dump())
            renew_before = max(
                policy.renew_before_seconds,
                window + MOVE_OUT_RENEWAL_LEAD_SECONDS if window else 0,
            )
            if row["management_mode"] != "manage" or expires - now > timedelta(
                seconds=renew_before
            ):
                continue
            lifecycle = snapshot.lifecycle_operations.get(row["id"], [])
            trigger = self.rotation.due(row, policy, lifecycle, now, expires)
            # A rollout already occupies this machine. Keep renewing the lease
            # it is still serving; do not start a node replacement beside it.
            release_moving = host_release_blocks_rotation(operations, release)
            started = (
                trigger is not None
                and not release_moving
                and await self.rotation.start(row, trigger, expires)
            )
            await self.rotation.remind(row, lifecycle, now, expires)
            if (
                self.rotation.replaces_renewal(row, policy, now)
                and not release_moving
                and (started or await self.rotation.replaces_in_time(lifecycle, now, expires))
            ):
                # A node at its maximum age is replaced rather than extended
                # while its move can finish in time. A stuck, cancelled or late
                # move, or none at all, still needs the lease.
                continue
            request_id = f"renew:{row['id']}:{int(expires.timestamp())}:{row['revision']}"
            if any(op["request_id"] == request_id for op in operations):
                continue
            request = ExtendComputeRequest(
                request_id=request_id,
                # Whole seconds: a provider that keeps expirations to the
                # second can then report exactly the target it was sent.
                target_expires_at=(
                    now + timedelta(seconds=policy.extend_to_horizon_seconds)
                ).replace(microsecond=0),
                reason=policy.reason,
            )
            try:
                await self.commands.extend(row["id"], request, actor="compute-renewal")
            except (ComputeRefusal, ValueError):
                # Concurrent schedulers may have already persisted a fixed target.
                logger.warning("compute_renewal_not_submitted", extra={"allocation_id": row["id"]})

    async def _queue_due_inspection(
        self,
        row: dict[str, Any],
        operations: list[dict[str, Any]],
        now: datetime,
        observation_seconds: int,
    ) -> None:
        """Queue one read once the last observation is due and none is already queued."""
        observed = _moment(row.get("observed_at"))
        due = observed is None or (now - observed).total_seconds() >= observation_seconds
        active_inspect = any(
            op["kind"] in {"inspect", "acquire", "import"}
            and op["state"] in {"pending", "running", "waiting", "unknown"}
            for op in operations
        )
        if due and not active_inspect:
            slot = int(now.timestamp()) // observation_seconds
            await self.commands.queue_inspection(row, request_key=f"inspect:{row['id']}:{slot}")

    async def _report_blocked_move_out(
        self, row: dict[str, Any], policy: RenewalPolicy, now: datetime, expires: datetime
    ) -> None:
        """Alert once, inside the move-out window, when groups will stay with the machine.

        Either the move cannot start (or continue) and the reason code says
        why, or it runs but some groups can never move: no checkpoint covers
        them, or their image cannot be pulled elsewhere. Reported while there
        is still time to act, unlike the reminder just before the lease ends.
        """
        window = move_out_window_seconds(policy.model_dump())
        if window == 0 or expires <= now or expires - now > timedelta(seconds=window):
            return
        key = f"{row['id']}:{int(expires.timestamp())}"
        if key in self._move_out_blocked_reported:
            return
        report = await self.repository.move_out_report(allocation_id=row["id"])
        if report["unmoved"] == 0:
            return
        if report["blocker"] is not None:
            reason = f"{report['blocker']}; {report['unmoved']} groups would stay"
        elif report["unmovable"]:
            reason = f"{report['unmovable']} of {report['unmoved']} groups cannot move"
        else:
            return
        self._move_out_blocked_reported.add(key)
        hosts = row.get("hosts") or []
        await self.rotation._notify(
            "compute_move_out_blocked",
            host=hosts[0]["host"] if hosts else row["name"],
            reason=reason,
        )

    async def _lose_reclaimed_groups(self, row: dict[str, Any]) -> None:
        """End the chats whose sandboxes went with a machine its provider reclaimed."""
        lost = await self.repository.lose_reclaimed_groups(
            allocation_id=row["id"], quiet_seconds=RECLAIMED_QUIET_SECONDS
        )
        if not lost:
            return
        hosts = row.get("hosts") or []
        await self.rotation._notify(
            "compute_groups_lost_with_server",
            host=hosts[0]["host"] if hosts else row["name"],
            reason=f"{len(lost)} groups lost",
        )

    async def _remind_unfinished_move_out(
        self, row: dict[str, Any], policy: RenewalPolicy, now: datetime, expires: datetime
    ) -> None:
        """Alert once when groups are still on a Host this close to its lease end.

        They are still running a turn, have no checkpoint, or had nowhere to
        go. Whatever has not moved when the lease ends stays with the machine.
        """
        if (
            move_out_window_seconds(policy.model_dump()) == 0
            or expires - now > timedelta(seconds=max(policy.stop_claims_before_seconds, 300))
            or expires <= now
        ):
            return
        key = f"{row['id']}:{int(expires.timestamp())}"
        if key in self._move_out_reminded:
            return
        remaining = await self.repository.unmoved_groups(allocation_id=row["id"])
        if remaining == 0:
            return
        self._move_out_reminded.add(key)
        hosts = row.get("hosts") or []
        await self.rotation._notify(
            "compute_move_out_unfinished",
            host=hosts[0]["host"] if hosts else row["name"],
            reason=f"{remaining} groups not moved",
        )

    async def execute(self, operation: dict[str, Any]) -> None:  # noqa: C901, PLR0912 complexity baseline, #771
        """Execute one claimed operation; public diagnostics contain no remote output."""
        try:
            kind = operation["kind"]
            if kind == "preflight":
                await self.acquisition.preflight(operation)
                return
            allocation = await self.repository.get_allocation(operation["allocation_id"])
            if allocation is None:
                raise ProviderError("allocation_not_found", "The allocation no longer exists")
            if kind in {"acquire", "import"}:
                await self.acquisition.acquire(operation, allocation)
            elif kind == "inspect":
                await self._inspect(operation, allocation)
                await self._finish(operation, "succeeded", "observed", "Resource state refreshed.")
            elif kind == "extend":
                await self._extend(operation, allocation)
            elif kind == "retire":
                await self.retirement.retire(operation, allocation)
            elif kind == "rotate":
                await self.rotation.rotate(operation, allocation)
            elif kind == "discard":
                await self.rotation.discard(operation, allocation)
            elif kind == "upgrade":
                await self._upgrade(operation, allocation)
            else:
                raise ProviderError("unsupported_operation", "Unknown compute operation")
        except (ProviderError, ComputeRefusal, ComputeSecretError) as exc:
            code = getattr(exc, "code", "credential_unavailable")
            retryable = bool(getattr(exc, "retryable", False))
            if operation.get("allocation_id") and operation["kind"] not in {"rotate", "upgrade"}:
                # A failed move says nothing about the old node's lease; recording
                # it would stop new chats there until the next observation.
                await self.repository.record_observation_error(
                    allocation_id=operation["allocation_id"], message=code, **self._fence(operation)
                )
                if code in {"resource_identity_changed", "machine_identity_changed"}:
                    await self.repository.set_admission_hold(
                        allocation_id=operation["allocation_id"],
                        purpose="lease_deadline",
                        source_id=f"identity:{operation['allocation_id']}",
                        scope="all_claims",
                        **self._fence(operation),
                    )
            if code == "result_unknown" and operation["kind"] in {"extend", "retire"}:
                await self._finish(
                    operation,
                    "waiting",
                    "confirming",
                    "The provider outcome is being checked.",
                    result={
                        "external_pending": True,
                        **provider_diagnostic_result(exc, operation, outcome="waiting"),
                    },
                    retry=30,
                )
            elif code == "result_unknown" and (
                operation["kind"] == "preflight"
                or operation.get("result", {}).get("resource_ref")
                or operation.get("step") in {"deploying", "verifying_host", "waiting_for_heartbeat"}
            ):
                await self._finish(
                    operation,
                    "waiting",
                    operation["step"],
                    "Connection and deployment readiness will be checked again.",
                    result=provider_diagnostic_result(exc, operation, outcome="waiting"),
                    retry=30,
                )
            elif code == "result_unknown":
                await self._finish(
                    operation,
                    "unknown",
                    operation["step"],
                    "The acquisition outcome is unknown. Identify the existing resource before continuing.",
                    result=provider_diagnostic_result(exc, operation, outcome="unknown"),
                )
            else:
                terminal_acquire = operation["kind"] == "acquire" and (
                    code == "resource_terminated"
                    or (
                        code == "resource_failed"
                        and operation.get("step") == "failed_resource_released"
                    )
                )
                await self._finish(
                    operation,
                    "failed" if terminal_acquire else "waiting" if retryable else "blocked",
                    operation["step"],
                    str(exc),
                    result={
                        **({"external_pending": False} if terminal_acquire else {}),
                        **provider_diagnostic_result(
                            exc,
                            operation,
                            outcome=(
                                "failed"
                                if terminal_acquire
                                else "waiting"
                                if retryable
                                else "blocked"
                            ),
                        ),
                        "blocked_reasons": [{"code": code, "message": str(exc)}],
                    },
                    retry=30 if retryable else None,
                )
            logger.warning(
                "compute_operation_needs_attention",
                extra={"operation_id": operation["id"], "reason": code},
            )
        except ValueError as exc:
            if isinstance(exc, StorageConflict) and exc.code is ConflictCode.OPERATION_LEASE_LOST:
                return
            # Only the store's typed refusals have a code; a provider's identity
            # change or missing registration URL arrives as a ProviderError above.
            code = exc.code if isinstance(exc, StorageConflict) else None
            if code is ConflictCode.RESOURCE_IDENTITY_CHANGED and operation.get("allocation_id"):
                await self.repository.set_admission_hold(
                    allocation_id=operation["allocation_id"],
                    purpose="lease_deadline",
                    source_id=f"identity:{operation['allocation_id']}",
                    scope="all_claims",
                    **self._fence(operation),
                )
            pre_submit = code is ConflictCode.AUTOSCALING_CONNECTION_UNVERIFIED
            await self._finish(
                operation,
                "blocked",
                operation["step"],
                (
                    "Autoscaling prerequisites changed before provider submission."
                    if pre_submit
                    else "Resource state changed; inspect the operation before retrying."
                ),
                result={
                    **({"external_pending": False} if pre_submit else {}),
                    "blocked_reasons": [
                        {
                            "code": str(code) if pre_submit else "resource_state_conflict",
                            "message": (
                                "Revalidate autoscaling connections and retry."
                                if pre_submit
                                else "Resource identity or state no longer matches this operation."
                            ),
                        }
                    ],
                },
            )

    async def _bootstrap_blocked(
        self,
        operation: dict[str, Any],
        reasons: tuple[str, ...],
        *,
        observation: ResourceObservation | None = None,
        provider_id: str | None = None,
    ) -> None:
        # Public diagnostics use our vocabulary, never remote exception/output text.
        blocked = [
            {"code": code, "message": _BOOTSTRAP_REASONS[code]}
            for code in reasons
            if code in _BOOTSTRAP_REASONS
        ]
        if not blocked:
            blocked = [
                {
                    "code": "bootstrap_not_ready",
                    "message": "Host deployment or readiness checks did not pass.",
                }
            ]
        result: dict[str, Any] = {"blocked_reasons": blocked}
        if observation is not None and provider_id == "cloudlab":
            error = ProviderError(
                "host_initialization_failed",
                "CloudLab resource is ready but host initialization did not pass.",
                diagnostics=cloudlab_observation_diagnostics(
                    observation, reason_code="experiment_ready"
                ),
            )
            result.update(provider_diagnostic_result(error, operation, outcome="blocked"))
        await self._finish(
            operation,
            "blocked",
            operation["step"],
            "Host deployment needs attention.",
            result=result,
        )

    async def _queue_host_release(
        self,
        row: dict[str, Any],
        operations: list[dict[str, Any]],
        release: HostRelease,
        now: datetime | None = None,
    ) -> None:
        """Queue one rollout when a managed host is not on this control-plane commit."""
        if row.get("management_mode") != "manage" or not row.get("external_id"):
            return
        if row.get("observed_state") != "ready":
            return
        profile = row.get("deployment_profile") or {}
        if not profile.get("deployment_sha") or not profile.get("source_sha256"):
            return
        hosts = row.get("hosts") or []
        if len(hosts) != 1:
            return
        if hosts[0].get("drain_fence_active"):
            await self._report_release_fence(row, release, profile, now or _now())
            return
        self._release_fenced_since.pop(row["id"], None)
        if any(
            op.get("kind") in _RELEASE_MUTATIONS and op.get("state") in _RELEASE_ACTIVE
            for op in operations
        ):
            return
        request_id = f"host-release:{row['id']}:{release.sha}"
        if any(op.get("request_id") == request_id for op in operations):
            return
        host = await self.host_pool.get_host(host=hosts[0]["host"])
        if host is None or host.get("status") != "online" or not host.get("enrolled"):
            return
        if self._on_release(host, profile, release):
            return
        parameters = {"allocation_id": row["id"], "deployment_sha": release.sha}
        await self.repository.create_operation(
            request_id=request_id,
            fingerprint=fingerprint("upgrade", parameters),
            kind="upgrade",
            allocation_id=row["id"],
            parameters=parameters,
            requested_by="compute-host-release",
        )
        logger.info(
            "compute_host_release_queued",
            extra={"allocation_id": row["id"], "deployment_sha": release.sha},
        )

    @staticmethod
    def _on_release(host: dict[str, Any], profile: dict[str, Any], release: HostRelease) -> bool:
        """Whether the Host runs this release and its stored profile pins it."""
        return shared_deployment_capability(release.sha) in (host.get("capabilities") or []) and (
            profile.get("deployment_sha") == release.sha
            and profile.get("source_sha256") == release.digest
        )

    async def _report_release_fence(
        self,
        row: dict[str, Any],
        release: HostRelease,
        profile: dict[str, Any],
        now: datetime,
    ) -> None:
        """Alert once when a deployment drain fence keeps a Host off this release.

        The rollout waits for the fence, which a deployment normally clears
        within one run. Left behind, it keeps the Host from taking work and
        from being rolled forward, so it never gains what a newer release
        adds, such as checkpoints its groups would need to move out.
        """
        hosts = row.get("hosts") or []
        host = await self.host_pool.get_host(host=hosts[0]["host"])
        if host is None or not host.get("enrolled") or self._on_release(host, profile, release):
            self._release_fenced_since.pop(row["id"], None)
            return
        since = self._release_fenced_since.setdefault(row["id"], now)
        key = f"{row['id']}:{release.sha}"
        if now - since < _RELEASE_FENCE_ALERT_AFTER or key in self._release_fence_reported:
            return
        self._release_fence_reported.add(key)
        await self.rotation._notify(
            "compute_host_release_blocked",
            host=hosts[0]["host"],
            reason="deployment drain fence active",
        )

    async def _upgrade(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:  # noqa: PLR0912 complexity baseline, #771
        """Deploy this control plane's host bundle and record that release.

        The host recipe builds the replacement before it drains runners, and it
        lets in-flight turns finish. Enrollment and package installation stay
        as they are. A rollout that stops advertising a generation is put back
        on the stored bundle before this operation gives up.
        """
        release = load_control_plane_host_release()
        target = (operation.get("parameters") or {}).get("deployment_sha")
        if release is None or release.sha != target:
            raise ProviderError(
                "automatic_release_mismatch",
                "The Host bundle does not match the running control-plane release.",
            )
        if allocation.get("desired_state") != "present" or allocation.get("release_committed_at"):
            await self._finish(
                operation, "succeeded", "skipped", "This server is no longer managed."
            )
            return
        if len(allocation.get("nodes") or []) != 1 or not allocation.get("deployment_profile"):
            raise ProviderError(
                "unsupported_node_count",
                "This deployment profile supports one node.",
            )
        profile = DeploymentProfile.model_validate(allocation["deployment_profile"])
        observation = await self._inspect(operation, allocation)
        if observation.state != "ready" or len(observation.nodes) != 1:
            await self._finish(
                operation,
                "waiting",
                "waiting_for_resource",
                "Waiting for the server to be ready before updating its host.",
                retry=30,
            )
            return
        allocation = await self._allocation(allocation["id"])
        node = allocation["nodes"][0]
        registered = await self.acquisition.registered_node(
            operation, allocation, node, observation.nodes[0]
        )
        if registered is None:
            return
        intent = self._bootstrap_intent(operation, allocation, node)
        host = await self.host_pool.get_host(host=intent.host)
        if host is None or not host.get("enrolled"):
            await self._finish(
                operation,
                "waiting",
                "waiting_for_heartbeat",
                "Waiting for the Host to register before updating it.",
                retry=30,
            )
            return
        if host.get("drain_fence_active"):
            await self._finish(
                operation,
                "waiting",
                "waiting_for_drain",
                "Waiting for the current deployment drain to finish.",
                retry=60,
            )
            return
        release_profile = profile.model_copy(
            update={
                "source_bundle": str(release.path),
                "source_sha256": release.digest,
                "deployment_sha": release.sha,
            }
        )
        advertises = shared_deployment_capability(release.sha) in (host.get("capabilities") or [])
        if not advertises:
            if host.get("status") != "online":
                await self._finish(
                    operation,
                    "waiting",
                    "waiting_for_heartbeat",
                    "Waiting for the Host to be online before updating it.",
                    retry=30,
                )
                return
            if not operation.get("result", {}).get("deployed"):
                await self._checkpoint(operation, "deploying")
                deployed = await self.bootstrap.deploy(
                    registered,
                    release_profile,
                    intent,
                    None,
                    prerequisites=False,
                    reenroll=False,
                )
                if not deployed.ready:
                    await self._recover_failed_release(
                        operation, registered, profile, intent, deployed
                    )
                    return
                await self._checkpoint(operation, "verifying_host", {"deployed": True})
            readiness = await self.bootstrap.readiness(registered, release_profile, intent)
            if not readiness.ready:
                await self._recover_failed_release(
                    operation, registered, profile, intent, readiness
                )
                return
            host = await self.host_pool.get_host(host=intent.host)
            if (
                host is None
                or host.get("status") != "online"
                or not host.get("enrolled")
                or shared_deployment_capability(release.sha) not in (host.get("capabilities") or [])
            ):
                await self._finish(
                    operation,
                    "waiting",
                    "waiting_for_heartbeat",
                    "Waiting for the Host to advertise the control-plane release.",
                    retry=15,
                )
                return
        await self._require_fresh_host(host)
        await self.repository.advance_host_release(
            allocation_id=allocation["id"],
            previous_sha=profile.deployment_sha,
            previous_digest=profile.source_sha256,
            deployment_sha=release.sha,
            source_sha256=release.digest,
            source_bundle=release.bundle,
            release_bundle_path=str(release.path),
            **self._fence(operation),
        )
        await self._finish(
            operation,
            "succeeded",
            "ready",
            "Host is running the control-plane release.",
            result={"deployment_sha": release.sha, "blocked_reasons": []},
        )

    async def _recover_failed_release(
        self,
        operation: dict[str, Any],
        node: NodeObservation,
        profile: DeploymentProfile,
        intent: BootstrapIntent,
        deployed: BootstrapResult,
    ) -> None:
        """Retry a rollout that left the previous generation up; otherwise restore it."""
        reasons = deployed.blocked_reasons
        # Only an explicit dark host is restored. A missing flag is not evidence
        # that the previous generation was taken down, and restoring would drain it.
        replaced = deployed.advertising is False
        if not replaced:
            if deployed.advertising is True:
                attempts = int(operation.get("result", {}).get("release_attempts") or 0) + 1
                if attempts < _RELEASE_ATTEMPTS:
                    await self._finish(
                        operation,
                        "waiting",
                        "deploying",
                        "The rollout stopped before replacing the running generation.",
                        result={"release_attempts": attempts},
                        retry=300,
                    )
                    return
            await self._bootstrap_blocked(operation, reasons)
            return
        if operation.get("result", {}).get("restore_attempted"):
            await self._bootstrap_blocked(operation, reasons)
            return
        await self._checkpoint(
            operation, "restoring", {"restore_attempted": True, "deployed": False}
        )
        restored = await self.bootstrap.deploy(
            node, profile, intent, None, prerequisites=False, reenroll=False
        )
        if not restored.ready:
            await self._bootstrap_blocked(operation, restored.blocked_reasons or reasons)
            return
        await self._finish(
            operation,
            "blocked",
            "restored",
            "The new release failed and the host is serving its previous release again.",
            result={"restored": True},
        )

    async def _require_fresh_host(self, host: dict[str, Any] | None) -> None:
        """Refuse to record a release the control plane cannot currently reach."""
        last = None if host is None else _moment(host.get("last_heartbeat_at"))
        if host is None or last is None or _now() - last > timedelta(seconds=90):
            raise ProviderError("host_stale", "The Host heartbeat is stale", retryable=True)
        sandbox_manager = sandbox_manager_for_host(host)
        if sandbox_manager is None:
            raise ProviderError(
                "sandbox_manager_unreachable",
                "The control plane cannot reach the Host sandbox manager",
                retryable=True,
            )
        try:
            await sandbox_manager.health()
        except SandboxManagerError:
            raise ProviderError(
                "sandbox_manager_unreachable",
                "The control plane cannot reach the Host sandbox manager",
                retryable=True,
            ) from None

    async def _inspect(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> ResourceObservation:
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        try:
            observation = await provider.inspect(self._ref(allocation))
        except ProviderError as exc:
            expired = self._expired_absence(allocation, exc)
            if expired is None:
                raise
            observation = expired
        await self._save_observation(operation, allocation, observation)
        return observation

    def _expired_absence(
        self, allocation: dict[str, Any], exc: ProviderError
    ) -> ResourceObservation | None:
        """Treat a missing resource as ended only once its own recorded lease has run out.

        Absence alone never proves deletion: visibility or credentials can
        change. Past the lease the provider has ended it regardless, so the
        recorded deadline is the evidence.
        """
        expires = _moment(allocation.get("expires_at"))
        if (
            exc.code != "resource_visibility_unknown"
            or not allocation.get("expiration_known")
            or expires is None
            or _now() < expires + _EXPIRY_GRACE
        ):
            return None
        return ResourceObservation(
            ref=self._ref(allocation),
            state="terminated",
            raw_state=EXPIRED_NOT_FOUND,
            observed_at=_now(),
            expires_at=expires,
            lease_known=True,
            scope_complete=True,
        )

    async def _extend(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        self.commands._require_managed(allocation, "extend")
        request = ExtendRequest.model_validate(
            {key: operation["parameters"][key] for key in ("target_expires_at", "reason")}
        )
        observation = await self._inspect(operation, allocation)
        if observation.expires_at is not None and request.granted_by(observation.expires_at):
            await self._finish(
                operation,
                "succeeded",
                "extended",
                "Provider confirmed the requested expiration.",
                result={
                    "target_expires_at": request.target_expires_at.isoformat(),
                    "actual_expires_at": observation.expires_at.isoformat(),
                    "external_pending": False,
                    "blocked_reasons": [],
                },
            )
            return
        if observation.state == "terminated":
            raise ProviderError("resource_terminated", "The resource can no longer be renewed")
        if operation.get("result", {}).get("extension_requested"):
            # Query before any retry. A known smaller deadline is a partial
            # approval, not success and not a reason to silently add more time.
            initial = _moment(operation.get("result", {}).get("extension_initial_expires_at"))
            progressed = (
                initial is not None
                and observation.expires_at is not None
                and observation.expires_at > initial
            )
            # A request recorded before its send time was kept is timed from now.
            requested_at = (
                _moment(operation.get("result", {}).get("extension_requested_at")) or _now()
            )
            if operation.get("result", {}).get("extension_response_received") or progressed:
                await self._finish(
                    operation,
                    "blocked",
                    "partial_extension",
                    "The provider approved a shorter lease than requested.",
                    result={
                        "external_pending": False,
                        "extension_requested": False,
                        "extension_response_received": False,
                        "actual_expires_at": observation.expires_at.isoformat()
                        if observation.expires_at
                        else None,
                    },
                )
            elif _now() - requested_at >= _EXTENSION_CONFIRMATION:
                # The deadline has not moved since the request. Stop holding
                # the allocation's other work: the target is absolute, so a
                # retry may safely send the same request again.
                message = (
                    "The provider neither applied nor refused the extension. "
                    "Check the provider, then retry to request the same expiration."
                )
                await self._finish(
                    operation,
                    "blocked",
                    "extension_unconfirmed",
                    message,
                    result={
                        "external_pending": False,
                        "extension_requested": False,
                        "extension_response_received": False,
                        "blocked_reasons": [{"code": "extension_unconfirmed", "message": message}],
                    },
                )
            else:
                await self._finish(
                    operation,
                    "waiting",
                    "confirming_extension",
                    "Waiting to confirm the previous extension request.",
                    result={
                        "external_pending": True,
                        "extension_requested_at": requested_at.isoformat(),
                    },
                    retry=30,
                )
            return
        await self._checkpoint(
            operation,
            "extending",
            {
                "extension_requested": True,
                "extension_requested_at": _now().isoformat(),
                "external_pending": True,
                "extension_response_received": False,
                "extension_initial_expires_at": observation.expires_at.isoformat()
                if observation.expires_at
                else None,
            },
        )
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        try:
            receipt = await provider.extend(self._ref(allocation), request)
        except (ProviderError, ComputeSecretError) as exc:
            if getattr(exc, "code", "credential_unavailable") != "result_unknown":
                await self._checkpoint(
                    operation,
                    "extension_rejected",
                    {"extension_requested": False, "external_pending": False},
                )
            raise
        await self._checkpoint(
            operation,
            "confirming_extension",
            {"extension_response_received": receipt.outcome == "confirmed"},
        )
        observed = receipt.observation or await provider.inspect(self._ref(allocation))
        await self._save_observation(operation, allocation, observed)
        if observed.expires_at is not None and request.granted_by(observed.expires_at):
            await self._finish(
                operation,
                "succeeded",
                "extended",
                "Provider confirmed the requested expiration.",
                result={
                    "target_expires_at": request.target_expires_at.isoformat(),
                    "actual_expires_at": observed.expires_at.isoformat(),
                    "external_pending": False,
                    "blocked_reasons": [],
                },
            )
        else:
            await self._finish(
                operation,
                "waiting",
                "confirming_extension",
                "Extension submitted; waiting for the actual expiration.",
                result={"external_pending": True},
                retry=30,
            )

    async def _save_observation(
        self,
        operation: dict[str, Any],
        allocation: dict[str, Any],
        observation: ResourceObservation,
    ) -> None:
        if allocation.get("desired_state") != "present" and observation.state != "terminated":
            expected = {
                (node["provider_node_id"], node["instance_identity"])
                for node in allocation.get("nodes", [])
            }
            actual = {(node.provider_node_id, node.instance_identity) for node in observation.nodes}
            committed_pending = (
                allocation.get("release_committed_at")
                and observation.state == "pending"
                and not observation.scope_complete
                and actual <= expected
            )
            if not committed_pending and (not observation.scope_complete or expected != actual):
                raise ProviderError(
                    "resource_identity_changed", "The allocation's complete node scope has changed"
                )
        saved = await self.repository.observe_allocation(
            allocation_id=allocation["id"],
            observation={
                "external_id": observation.ref.external_id,
                "configuration": observation.ref.config,
                "observed_state": observation.state,
                "provider_state": observation.raw_state,
                "expires_at": observation.expires_at,
                "expiration_known": observation.lease_known,
                "scope_complete": observation.scope_complete,
                "nodes": [
                    {
                        "provider_node_id": node.provider_node_id,
                        "instance_identity": node.instance_identity,
                        "name": node.display_name,
                        "connection": node.connection.model_dump(mode="json")
                        if node.connection
                        else {},
                    }
                    for node in observation.nodes
                ],
            },
            **self._fence(operation),
        )
        if (
            saved.get("observed_state") in {"terminated", "absent"}
            and observation.state != "terminated"
        ):
            # The resource's terminal proof stood; this stale read sets no holds.
            return
        policy = RenewalPolicy.model_validate(allocation.get("policy") or {})
        scope = None
        if observation.expires_at is not None:
            remaining = (observation.expires_at - _now()).total_seconds()
            if remaining <= policy.stop_claims_before_seconds:
                scope = "all_claims"
            elif remaining <= policy.stop_new_groups_before_seconds:
                scope = "new_groups"
        await self.repository.set_admission_hold(
            allocation_id=allocation["id"],
            purpose="lease_deadline",
            source_id=allocation["id"],
            scope=scope,
            **self._fence(operation),
        )
        # Within the move-out window every claim stops here, so each chat's
        # next turn is taken by another Host from its group's checkpoint, and
        # the move-out sweep stops and hands over the idle groups.
        window = move_out_window_seconds(policy.model_dump())
        await self.repository.set_move_out_hold(
            allocation_id=allocation["id"],
            due=(
                window > 0
                and observation.expires_at is not None
                and (observation.expires_at - _now()).total_seconds() <= window
            ),
            **self._fence(operation),
        )

    async def _allocation(self, allocation_id: str) -> dict[str, Any]:
        row = await self.repository.get_allocation(allocation_id)
        if row is None:
            raise StorageConflict(ConflictCode.ALLOCATION_NOT_FOUND)
        return row

    @staticmethod
    def _ref(allocation: dict[str, Any]) -> ResourceRef:
        return ResourceRef(
            external_id=allocation["external_id"], config=allocation["configuration"]
        )

    @staticmethod
    def _request(allocation: dict[str, Any]) -> AcquireRequest:
        return AcquireRequest(mode=allocation["mode"], config=allocation["configuration"])

    def _intent(self, operation: dict[str, Any], allocation: dict[str, Any]) -> OperationIntent:
        return OperationIntent(
            operation_id=operation["id"],
            allocation_id=allocation["id"],
            requested_at=operation["created_at"],
            request_fingerprint=operation["fingerprint"],
            acquisition=self._request(allocation),
            known_external_id=allocation.get("external_id"),
        )

    @staticmethod
    def _node(node: dict[str, Any]) -> NodeObservation:
        return NodeObservation(
            provider_node_id=node["provider_node_id"],
            instance_identity=node["instance_identity"],
            display_name=node["name"],
            connection=node.get("connection") or None,
        )

    @staticmethod
    def _bootstrap_intent(
        operation: dict[str, Any], allocation: dict[str, Any], node: dict[str, Any]
    ) -> BootstrapIntent:
        # A node that took a Host over keeps that Host's name and namespace.
        return BootstrapIntent(
            operation_id=operation["id"],
            allocation_id=allocation["id"],
            node_id=node["id"],
            host=allocation.get("slot_host") or "compute-" + node["id"].replace("-", ""),
            namespace=allocation.get("slot_namespace"),
        )

    @staticmethod
    def _fence(operation: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": operation["id"],
            "generation": operation["generation"],
            "worker_id": operation["worker_id"],
        }

    async def _checkpoint(
        self, operation: dict[str, Any], step: str, result: dict[str, Any] | None = None
    ) -> None:
        if not await self.repository.checkpoint_operation(
            **self._fence(operation), step=step, result=result
        ):
            raise StorageConflict(ConflictCode.OPERATION_LEASE_LOST)
        operation["step"] = step
        operation["result"] = {**operation.get("result", {}), **(result or {})}

    async def _finish(
        self,
        operation: dict[str, Any],
        state: str,
        step: str,
        message: str,
        *,
        result: dict[str, Any] | None = None,
        retry: float | None = None,
    ) -> None:
        await self.repository.finish_operation(
            **self._fence(operation),
            state=state,
            step=step,
            message=message,
            result=result,
            retry_after_seconds=retry,
        )
