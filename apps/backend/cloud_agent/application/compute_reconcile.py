"""Durable acquisition, deployment, renewal, and retirement steps.

Every side effect has a preceding checkpoint. A recovered acquisition is
queried, never blindly recreated. The execution lease fences database writes;
fixed resource identity and request parameters bound the external effects.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from cloud_agent.application.compute_rotation import ComputeRotation
from cloud_agent.application.compute_setup import HostRelease, load_control_plane_host_release
from cloud_agent.application.host_commands import HostCommands
from cloud_agent.compute_config import current_observation_seconds, current_registration_url
from cloud_agent.compute_secrets import ComputeSecretError
from cloud_agent.domain.compute import (
    MOVE_OUT_RENEWAL_LEAD_SECONDS,
    ComputeRefusal,
    fingerprint,
    move_out_window_seconds,
    provider_resource_absent,
)
from cloud_agent.host_pool import hash_secret
from cloud_agent.logging_utils import get_logger
from cloud_agent.ports.external.compute_provider import (
    AcquireRequest,
    ExtendRequest,
    NodeObservation,
    NodeRegistration,
    OperationIntent,
    ProviderError,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
)
from cloud_agent.ports.external.host_bootstrap import (
    BootstrapIntent,
    BootstrapResult,
    DeploymentProfile,
)
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager_routing import sandbox_manager_for_host
from cloud_agent.schemas_compute import ExtendComputeRequest, RenewalPolicy
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
_PREFLIGHT_CHECKS = {
    "supported_os",
    "architecture",
    "privileged_deploy",
    "kvm",
    "docker",
    "compose",
    "data_mount_available",
    "data_mount_conflict_free",
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
# After its provider confirms a machine is gone, how long its Host must have
# been silent before the groups it still holds are marked lost.
_RECLAIMED_QUIET_SECONDS = 600


# How long past its recorded deadline a missing resource is taken as ended.
_EXPIRY_GRACE = timedelta(minutes=10)
# How long an extension the provider neither applies nor refuses is awaited.
# Every other operation of the allocation waits for it meanwhile; afterwards
# it blocks, so a move can start and a retry can send the same target again.
_EXTENSION_CONFIRMATION = timedelta(minutes=10)
# A node registers at first boot; provisioning plus boot fit well inside this.
_REGISTRATION_TTL_SECONDS = 2 * 3600
_HOST_KEY_PREFERENCE = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)


def _preferred_host_key(keys: list[str]) -> str:
    """Pin the strongest key type the node registered."""
    ranked = sorted(
        keys,
        key=lambda key: (
            _HOST_KEY_PREFERENCE.index(key.split()[0])
            if key.split()[0] in _HOST_KEY_PREFERENCE
            else len(_HOST_KEY_PREFERENCE)
        ),
    )
    if not ranked or ranked[0].split()[0] not in _HOST_KEY_PREFERENCE:
        raise ProviderError("registration_invalid", "The node registered no supported host key")
    return ranked[0]


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        self._move_out_reminded: set[str] = set()
        self._move_out_blocked_reported: set[str] = set()
        self._release_fenced_since: dict[str, datetime] = {}
        self._release_fence_reported: set[str] = set()

    async def schedule(self) -> None:
        """Observe leases and submit renewals even while retirement is blocked."""
        now = _now()
        observation_seconds = current_observation_seconds(self.commands.configuration)
        release = load_control_plane_host_release()
        for row in await self.repository.list_allocations():
            if row.get("observed_state") == "terminated":
                await self._lose_reclaimed_groups(row)
            if row.get("release_committed_at") or row.get("observed_state") in {
                "terminated",
                "absent",
            }:
                continue
            operations = await self.repository.list_operations(allocation_id=row["id"])
            if row.get("external_id"):
                observed = _moment(row.get("observed_at"))
                due = observed is None or (now - observed).total_seconds() >= observation_seconds
                active_inspect = any(
                    op["kind"] in {"inspect", "acquire", "import"}
                    and op["state"] in {"pending", "running", "waiting", "unknown"}
                    for op in operations
                )
                if due and not active_inspect:
                    slot = int(now.timestamp()) // observation_seconds
                    await self.commands.queue_inspection(
                        row, request_key=f"inspect:{row['id']}:{slot}"
                    )
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
            lifecycle = await self.repository.list_operations(
                allocation_id=row["id"], kinds=["extend", "rotate"]
            )
            trigger = self.rotation.due(row, policy, lifecycle, now, expires)
            # A rollout already occupies this machine. Keep renewing the lease
            # it is still serving; do not start a node replacement beside it.
            release_moving = self._host_release_blocks_rotation(operations, release)
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
            allocation_id=row["id"], quiet_seconds=_RECLAIMED_QUIET_SECONDS
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

    async def execute(self, operation: dict[str, Any]) -> None:
        """Execute one claimed operation; public diagnostics contain no remote output."""
        try:
            kind = operation["kind"]
            if kind == "preflight":
                await self._preflight(operation)
                return
            allocation = await self.repository.get_allocation(operation["allocation_id"])
            if allocation is None:
                raise ProviderError("allocation_not_found", "The allocation no longer exists")
            if kind in {"acquire", "import"}:
                await self._acquire(operation, allocation)
            elif kind == "inspect":
                await self._inspect(operation, allocation)
                await self._finish(operation, "succeeded", "observed", "Resource state refreshed.")
            elif kind == "extend":
                await self._extend(operation, allocation)
            elif kind == "retire":
                await self._retire(operation, allocation)
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
                    result={"external_pending": True},
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
                    retry=30,
                )
            elif code == "result_unknown":
                await self._finish(
                    operation,
                    "unknown",
                    operation["step"],
                    "The acquisition outcome is unknown. Identify the existing resource before continuing.",
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
                        "blocked_reasons": [{"code": code, "message": str(exc)}],
                    },
                    retry=30 if retryable else None,
                )
            logger.warning(
                "compute_operation_needs_attention",
                extra={"operation_id": operation["id"], "reason": code},
            )
        except ValueError as exc:
            code = str(exc)
            if code == "operation_lease_lost":
                return
            if code in {"resource_identity_changed", "machine_identity_changed"} and operation.get(
                "allocation_id"
            ):
                await self.repository.set_admission_hold(
                    allocation_id=operation["allocation_id"],
                    purpose="lease_deadline",
                    source_id=f"identity:{operation['allocation_id']}",
                    scope="all_claims",
                    **self._fence(operation),
                )
            pre_submit = code in {
                "autoscaling_connection_unverified",
                "registration_unavailable",
            }
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
                            "code": code if pre_submit else "resource_state_conflict",
                            "message": (
                                "Revalidate autoscaling connections and retry."
                                if code == "autoscaling_connection_unverified"
                                else "Resource identity or state no longer matches this operation."
                            ),
                        }
                    ],
                },
            )

    async def _preflight(self, operation: dict[str, Any]) -> None:
        parameters = operation["parameters"]
        entry = self.commands.registry.get(parameters["provider_id"])
        config = parameters["validated_configuration"]
        await self._checkpoint(operation, "checking_configuration")
        if parameters["mode"] == "provision":
            await entry.validate_connection(config["credential_ref"], {})
            await self._finish(
                operation,
                "succeeded",
                "preflight_complete",
                "Credentials and configuration checked. Capacity and SSH readiness are checked after allocation.",
                result={
                    "checks": {
                        "credentials": True,
                        "configuration": True,
                        "capacity_reserved": False,
                    }
                },
            )
            return
        # Attachment is read-only; it neither registers inventory nor installs Host software.
        intent = self._intent(
            operation, {"id": operation["id"], "mode": "attach", "configuration": config}
        )
        receipt = await entry.provider.acquire(AcquireRequest(mode="attach", config=config), intent)
        observation = receipt.observation
        if observation is None or observation.state != "ready" or len(observation.nodes) != 1:
            raise ProviderError("resource_not_ready", "Preflight needs one ready node")
        checked = await self.bootstrap.preflight(
            observation.nodes[0], DeploymentProfile.model_validate(parameters["profile"])
        )
        if not checked.ready:
            await self._bootstrap_blocked(operation, checked.blocked_reasons)
            return
        await self._finish(
            operation,
            "succeeded",
            "preflight_complete",
            "Connection, identity and deployment prerequisites checked.",
            result={
                "checks": {
                    key: value for key, value in checked.checks.items() if key in _PREFLIGHT_CHECKS
                }
            },
        )

    async def _acquire(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        observation: ResourceObservation | None
        if allocation.get("external_id"):
            observation = await provider.inspect(self._ref(allocation))
        elif operation.get("result", {}).get("resource_ref"):
            observation = await provider.inspect(
                ResourceRef.model_validate(operation["result"]["resource_ref"])
            )
        elif operation.get("step") != "pending":
            resolution = await provider.recover_acquire(self._intent(operation, allocation))
            if resolution.outcome == "not_created":
                await self._checkpoint(operation, "acquiring")
                receipt = await self._submit_acquisition(operation, allocation)
            elif resolution.receipt is not None:
                receipt = resolution.receipt
            else:
                raise ProviderError(
                    "result_unknown", "The provider cannot identify the previous acquisition"
                )
            observation = await self._receipt_observation(operation, allocation, receipt)
        else:
            await self._checkpoint(operation, "acquiring")
            receipt = await self._submit_acquisition(operation, allocation)
            observation = await self._receipt_observation(operation, allocation, receipt)
        if observation is None:
            raise ProviderError(
                "result_unknown", "The provider did not return an identifiable resource"
            )
        await self._save_observation(operation, allocation, observation)
        if observation.state == "terminated":
            raise ProviderError("resource_terminated", "The allocated resource has terminated")
        if observation.state == "failed":
            await self._release_failed_acquisition(operation, allocation)
            raise ProviderError(
                "resource_failed",
                "The provider could not set up this resource; it was released",
            )
        if observation.state != "ready":
            await self._finish(
                operation,
                "waiting",
                "waiting_for_resource",
                "Waiting for the provider to make the resource ready.",
                result={"external_pending": True},
                retry=30,
            )
            return
        await self._checkpoint(operation, "resource_ready", {"external_pending": False})
        if operation["kind"] == "import":
            await self._finish(
                operation,
                "succeeded",
                "observed",
                "Resource imported for observation; no Host was deployed.",
            )
            return
        if len(observation.nodes) != 1:
            raise ProviderError(
                "unsupported_node_count",
                "This deployment profile supports one node; the full allocation remains visible for review",
            )
        allocation = await self._allocation(allocation["id"])
        node = allocation["nodes"][0]
        registered = await self._registered_node(operation, allocation, node, observation.nodes[0])
        if registered is None:
            return
        observed_node = registered
        profile = DeploymentProfile.model_validate(allocation["deployment_profile"])
        intent = self._bootstrap_intent(operation, allocation, node)
        if operation.get("parameters", {}).get("rotation_operation_id"):
            # A replacement node: staged and filled with a copy, never started here.
            await self.rotation.stage_candidate(
                operation, allocation, observed_node, profile, intent
            )
            return
        await self.repository.prepare_bootstrap_binding(
            node_id=node["id"], host=intent.host, **self._fence(operation)
        )
        host = await self.host_pool.get_host(host=intent.host)
        if not operation.get("result", {}).get("deployed"):
            await self._checkpoint(operation, "deploying")
            enrollment: SecretStr | None = None
            if host is None or not host.get("enrolled"):
                token, _ = await HostCommands(self.host_pool).enroll_token(
                    host=intent.host,
                    ttl_seconds=max(900, profile.timeout_seconds + 300),
                    actor=operation.get("requested_by"),
                )
                enrollment = SecretStr(token)
            deployed = await self.bootstrap.deploy(observed_node, profile, intent, enrollment)
            if not deployed.ready:
                await self._bootstrap_blocked(operation, deployed.blocked_reasons)
                return
            await self._checkpoint(operation, "verifying_host", {"deployed": True})
        readiness = await self.bootstrap.readiness(observed_node, profile, intent)
        if not readiness.ready:
            await self._bootstrap_blocked(operation, readiness.blocked_reasons)
            return
        host = await self.host_pool.get_host(host=intent.host)
        if host is None or host.get("status") != "online" or not host.get("enrolled"):
            await self._finish(
                operation,
                "waiting",
                "waiting_for_heartbeat",
                "Waiting for the deployed Host to register and heartbeat.",
                retry=15,
            )
            return
        last = _moment(host.get("last_heartbeat_at"))
        if last is None or _now() - last > timedelta(seconds=90):
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
        if not await self.repository.clear_bootstrap_hold(
            node_id=node["id"], host=intent.host, **self._fence(operation)
        ):
            raise ValueError("bootstrap_binding_changed")
        await self._finish(
            operation,
            "succeeded",
            "ready",
            "Server is ready. It can now be selected as the global sandbox location.",
            result={"nodes_ready": 1, "external_pending": False, "blocked_reasons": []},
        )

    async def _bootstrap_blocked(self, operation: dict[str, Any], reasons: tuple[str, ...]) -> None:
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
        await self._finish(
            operation,
            "blocked",
            operation["step"],
            "Host deployment needs attention.",
            result={"blocked_reasons": blocked},
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

    @staticmethod
    def _host_release_blocks_rotation(
        operations: list[dict[str, Any]], release: HostRelease | None
    ) -> bool:
        """Keep a node where it is while its release is moving or needs attention."""
        if release is None:
            return False
        return any(
            op.get("kind") == "upgrade"
            and (op.get("parameters") or {}).get("deployment_sha") == release.sha
            and op.get("state") not in {"succeeded", "cancelled"}
            for op in operations
        )

    async def _upgrade(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
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
        registered = await self._registered_node(operation, allocation, node, observation.nodes[0])
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
            raw_state="expired_not_found",
            observed_at=_now(),
            expires_at=expires,
            lease_known=True,
            scope_complete=True,
        )

    @staticmethod
    def _registers_host_keys(allocation: dict[str, Any]) -> bool:
        return (allocation.get("configuration") or {}).get("host_key_source") == "registration"

    async def _open_registration(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> NodeRegistration:
        """Mint the one-time token this acquisition's node registers its host keys with.

        Each submission gets a fresh token; only the newest one of an
        allocation can register, and only its hash is stored.
        """
        url = current_registration_url(self.commands.configuration)
        if not url:
            raise ProviderError(
                "registration_unavailable",
                "Set the public API address before nodes can register their host keys",
            )
        token = secrets.token_urlsafe(32)
        await self.repository.create_node_registration(
            allocation_id=allocation["id"],
            token_hash=hash_secret(token),
            ttl_seconds=_REGISTRATION_TTL_SECONDS,
            **self._fence(operation),
        )
        return NodeRegistration(url=url, token=SecretStr(token))

    async def _registered_node(
        self,
        operation: dict[str, Any],
        allocation: dict[str, Any],
        node: dict[str, Any],
        observed: NodeObservation,
    ) -> NodeObservation | None:
        """Return the node with the host key it registered, or None while waiting.

        Nodes trusted through an administrator's verified list are unchanged.
        A registering node is reached over SSH only once its key is pinned.
        """
        connection = observed.connection
        if connection is None or not self._registers_host_keys(allocation):
            return observed
        pinned = (node.get("connection") or {}).get("host_key")
        if not pinned:
            registration = await self.repository.node_registration(allocation_id=allocation["id"])
            missed = (
                registration is not None
                and not registration.get("registered_at")
                and registration.get("expired")
            )
            retried = any(
                reason.get("code") == "registration_expired"
                for reason in operation.get("result", {}).get("blocked_reasons") or []
            )
            # An administrator retried after the node missed its window. The
            # node holds this token from its creation and posts it at every
            # boot, so the same registration gets another window.
            if (
                missed
                and retried
                and await self.repository.reopen_node_registration(
                    allocation_id=allocation["id"],
                    ttl_seconds=_REGISTRATION_TTL_SECONDS,
                    **self._fence(operation),
                )
            ):
                registration = {**(registration or {}), "expired": False}
            if registration is None or (
                not registration.get("registered_at") and registration.get("expired")
            ):
                raise ProviderError(
                    "registration_expired",
                    "The node did not register its SSH host keys in time. Retry to give it "
                    "two more hours, then restart the node; or release the server.",
                )
            if not registration.get("registered_at"):
                await self._finish(
                    operation,
                    "waiting",
                    "waiting_for_registration",
                    "Waiting for the node to register its SSH host keys.",
                    result={"blocked_reasons": []},
                    retry=15,
                )
                return None
            pinned = _preferred_host_key(registration["host_keys"])
            await self.repository.record_host_key(
                node_id=node["id"], host_key=pinned, **self._fence(operation)
            )
        return observed.model_copy(
            update={"connection": connection.model_copy(update={"host_key": pinned})}
        )

    async def _release_failed_acquisition(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> None:
        """Delete a resource the provider failed to set up.

        No Host ever ran there: deployment starts only once a resource is ready.
        Left alone it would hold machines and this deployment setup until its
        lease ends.
        """
        if allocation["mode"] != "provision" or allocation.get("management_mode") != "manage":
            return
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        if not provider.capabilities().can_release:
            return
        # The caller's copy predates the identity this acquisition just saved.
        allocation = await self._allocation(allocation["id"])
        await self._checkpoint(operation, "releasing_failed", {"external_pending": True})
        receipt = await provider.release(self._ref(allocation), self._intent(operation, allocation))
        observation = receipt.observation or await provider.inspect(self._ref(allocation))
        await self._save_observation(operation, allocation, observation)
        if observation.state != "terminated":
            raise ProviderError(
                "release_pending",
                "Waiting for the provider to remove the failed resource",
                retryable=True,
            )
        await self._checkpoint(operation, "failed_resource_released", {"external_pending": False})

    async def _submit_acquisition(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> ProviderReceipt:
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        try:
            request = self._request(allocation)
            if self._registers_host_keys(allocation):
                request = request.model_copy(
                    update={"registration": await self._open_registration(operation, allocation)}
                )
            await self.repository.assert_autoscale_acquire_connections_verified(
                allocation_id=allocation["id"], **self._fence(operation)
            )
        except (ProviderError, ComputeSecretError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc) == "operation_lease_lost":
                raise
            # Nothing has reached the provider yet, so every failure here is
            # a definite refusal and can safely release an autoscaling slot.
            await self._checkpoint(operation, "pending", {"external_pending": False})
            raise
        try:
            return await provider.acquire(request, self._intent(operation, allocation))
        except (ProviderError, ComputeSecretError) as exc:
            if getattr(exc, "code", "credential_unavailable") != "result_unknown":
                # A definite rejection is safe to retry after credentials or
                # capacity are repaired. Ambiguous submissions keep the fence.
                await self._checkpoint(operation, "pending", {"external_pending": False})
            raise

    async def _receipt_observation(
        self, operation: dict[str, Any], allocation: dict[str, Any], receipt: ProviderReceipt
    ) -> ResourceObservation | None:
        if receipt.resource is not None:
            await self._checkpoint(
                operation,
                "resource_identified",
                {
                    "resource_ref": receipt.resource.model_dump(mode="json"),
                    "external_id": receipt.resource.external_id,
                },
            )
        if receipt.observation is not None:
            return receipt.observation
        if receipt.resource is not None:
            provider = self.commands.registry.get(allocation["provider_id"]).provider
            return await provider.inspect(receipt.resource)
        return None

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
                    {
                        "extension_requested": False,
                        "external_pending": False,
                    },
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

    async def _settle_terminal_retirement(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> None:
        """Commit local fencing after the provider is already terminal."""
        await self.repository.commit_release(
            allocation_id=allocation["id"], **self._fence(operation)
        )
        await self._finish(
            operation,
            "succeeded",
            "released",
            "Provider confirmed that the resource is terminated.",
            result={"external_pending": False, "blocked_reasons": []},
        )

    async def _retire(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        if not allocation.get("release_committed_at"):
            if allocation.get("desired_state") == "absent" and allocation.get("observed_state") in {
                "terminated",
                "absent",
            }:
                await self._settle_terminal_retirement(operation, allocation)
                return
            self.commands._require_managed(allocation, "release")
            await self.repository.begin_retirement(
                allocation_id=allocation["id"], **self._fence(operation)
            )
            allocation = await self._allocation(allocation["id"])
            await self._checkpoint(operation, "checking_retention", {"external_pending": False})
            if allocation.get("external_id"):
                observation = await self._inspect(operation, allocation)
                if observation.state == "terminated":
                    await self._settle_terminal_retirement(operation, allocation)
                    return
                allocation = await self._allocation(allocation["id"])
            impact = await self.repository.retirement_impact(allocation["id"])
            meaningful = [
                {"code": code, "message": code.replace("_", " ")}
                for code in impact["blocked_reasons"]
                if code not in {"inventory_unverified", "host_observation_stale"}
            ]
            if meaningful:
                await self._finish(
                    operation,
                    "waiting",
                    "waiting_for_retention",
                    "Resource release is waiting for its dependencies to be cleared.",
                    result={"blocked_reasons": meaningful, "external_pending": False},
                    retry=30,
                )
                return
            # An empty node list cannot pass the inventory scan: that scan
            # refuses to call zero nodes "empty". There is no machine to scan.
            if not provider_resource_absent(allocation):
                await self._verify_empty_inventory(operation, allocation)
            committed = await self.repository.commit_release(
                allocation_id=allocation["id"], **self._fence(operation)
            )
            if committed.get("blocked_reasons"):
                reasons = [
                    {"code": code, "message": code.replace("_", " ")}
                    for code in committed["blocked_reasons"]
                ]
                await self._finish(
                    operation,
                    "waiting",
                    "waiting_for_retention",
                    "Resource dependencies changed during the release checks.",
                    result={"blocked_reasons": reasons, "external_pending": False},
                    retry=30,
                )
                return
            allocation = await self._allocation(allocation["id"])
        if provider_resource_absent(allocation):
            await self._release_absent_provider_resource(operation, allocation)
            return
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        observation = await self._inspect(operation, allocation)
        if observation.state == "terminated":
            await self._finish(
                operation,
                "succeeded",
                "released",
                "Provider confirmed that the resource is terminated.",
                result={"external_pending": False, "blocked_reasons": []},
            )
            return
        if operation.get("result", {}).get("release_requested") and observation.state != "ready":
            await self._finish(
                operation,
                "waiting",
                "confirming_release",
                "Release was submitted; checking provider state before declaring success.",
                result={"external_pending": True},
                retry=30,
            )
            return
        await self._checkpoint(
            operation, "releasing", {"release_requested": True, "external_pending": True}
        )
        try:
            receipt = await provider.release(
                self._ref(allocation), self._intent(operation, allocation)
            )
        except (ProviderError, ComputeSecretError) as exc:
            if getattr(exc, "code", "credential_unavailable") != "result_unknown":
                await self._checkpoint(
                    operation,
                    "release_rejected",
                    {
                        "release_requested": False,
                        "external_pending": False,
                    },
                )
            raise
        observation = receipt.observation or await provider.inspect(self._ref(allocation))
        await self._save_observation(operation, allocation, observation)
        if observation.state == "terminated":
            await self._finish(
                operation,
                "succeeded",
                "released",
                "Provider confirmed that the resource is terminated.",
                result={"external_pending": False, "blocked_reasons": []},
            )
        else:
            await self._finish(
                operation,
                "waiting",
                "confirming_release",
                "Waiting for provider termination confirmation.",
                result={"external_pending": True},
                retry=30,
            )

    async def _release_absent_provider_resource(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> None:
        """Finish retirement of a server whose provider resource was never observed.

        Inspect and delete both require an experiment id. A missing profile, or
        a create cancelled before submission, leaves none: recording absence
        locally is the whole release. A create the provider answered left its
        id with the acquisition, and that experiment is deleted. No Host ran
        there: deployment starts only on a node an observation recorded.
        """
        ref = await self._provider_resource(allocation)
        if ref is not None:
            observation = await self._delete_by_identity(operation, allocation, ref)
            if observation.state != "terminated":
                await self._finish(
                    operation,
                    "waiting",
                    "confirming_release",
                    "Waiting for provider termination confirmation.",
                    result={"external_pending": True},
                    retry=30,
                )
                return
            await self._save_observation(operation, allocation, observation)
            await self._finish(
                operation,
                "succeeded",
                "released",
                "Provider confirmed that the resource is terminated.",
                result={"external_pending": False, "blocked_reasons": []},
            )
            return
        await self.repository.observe_allocation(
            allocation_id=allocation["id"],
            observation={
                "observed_state": "absent",
                "provider_state": "not_created",
                "scope_complete": True,
                "expiration_known": False,
            },
            **self._fence(operation),
        )
        await self._finish(
            operation,
            "succeeded",
            "released",
            "No provider resource exists for this server, so it was released locally.",
            result={"external_pending": False, "blocked_reasons": []},
        )

    async def _provider_resource(self, allocation: dict[str, Any]) -> ResourceRef | None:
        """The provider resource of this allocation, or None if none was ever named.

        Its identity is the allocation's once an observation recorded it. Until
        then only the acquisition the provider answered keeps it.
        """
        if allocation.get("external_id"):
            return self._ref(allocation)
        for prior in await self.repository.list_operations(
            allocation_id=allocation["id"], kinds=["acquire", "import"]
        ):
            result = prior.get("result") or {}
            if result.get("resource_ref"):
                return ResourceRef.model_validate(result["resource_ref"])
            named = result.get("external_id") or result.get("resolved_external_id")
            if named:
                return ResourceRef(external_id=named, config=allocation["configuration"])
        return None

    async def _delete_by_identity(
        self, operation: dict[str, Any], allocation: dict[str, Any], ref: ResourceRef
    ) -> ResourceObservation:
        """Delete one resource by its exact identity and return what the provider reports.

        Its node scope is not reviewed: the caller knows no Host of ours ran there.
        """
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        observation = await provider.inspect(ref)
        if observation.state != "terminated":
            await self._checkpoint(
                operation, "releasing", {"release_requested": True, "external_pending": True}
            )
            receipt = await provider.release(ref, self._intent(operation, allocation))
            observation = receipt.observation or await provider.inspect(ref)
        return observation

    async def _verify_empty_inventory(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> None:
        if not allocation.get("deployment_profile"):
            raise ProviderError(
                "inventory_unavailable",
                "Attach a deployment profile and configured SSH connection before releasing this resource",
            )
        profile = DeploymentProfile.model_validate(allocation["deployment_profile"])
        for host in allocation.get("hosts", []):
            sandbox_manager = sandbox_manager_for_host(host)
            if sandbox_manager is None:
                continue  # A trusted full-node scan below must prove this host empty.
            try:
                if await sandbox_manager.list_sandboxes():
                    raise ProviderError(
                        "retained_groups_present",
                        "The sandbox manager still reports retained sandbox state",
                    )
            except SandboxManagerError:
                continue  # Never infer emptiness; require the full-node scan below.
        # This control plane reaches a registering node only through the key
        # its registration supplied, and binds a Host only after pinning it.
        # A node that never registered, with no key and no Host, never ran
        # anything of ours, and without a trusted key it cannot be scanned.
        never_registered = False
        if self._registers_host_keys(allocation):
            registration = await self.repository.node_registration(allocation_id=allocation["id"])
            never_registered = registration is None or not registration.get("registered_at")
        bound = {host.get("compute_node_id") for host in allocation.get("hosts", [])}
        for node in allocation["nodes"]:
            if (
                never_registered
                and not (node.get("connection") or {}).get("host_key")
                and node["id"] not in bound
            ):
                continue
            proof = await self.bootstrap.inventory(
                self._node(node), profile, self._bootstrap_intent(operation, allocation, node)
            )
            if (
                not proof.complete
                or proof.retained_containers
                or proof.unknown_containers
                or proof.orphan_state
            ):
                raise ProviderError(
                    "inventory_not_empty",
                    "Stopped containers, unrecognized containers, or retained files prevent release",
                )
        recorded = await self.repository.record_inventory(
            allocation_id=allocation["id"],
            hosts=[h["host"] for h in allocation.get("hosts", [])],
            verified_node_ids=[node["id"] for node in allocation["nodes"]],
            empty=True,
            **self._fence(operation),
        )
        if not recorded:
            raise ValueError("inventory_changed")

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
        await self.repository.observe_allocation(
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
            raise ValueError("allocation_not_found")
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
            raise ValueError("operation_lease_lost")
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
