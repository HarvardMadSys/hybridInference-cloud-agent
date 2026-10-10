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
from cloud_agent.application.host_commands import HostCommands
from cloud_agent.broker_routing import broker_for_host
from cloud_agent.compute_secrets import ComputeSecretError
from cloud_agent.domain.compute import ComputeRefusal
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
from cloud_agent.ports.external.host_bootstrap import BootstrapIntent, DeploymentProfile
from cloud_agent.schemas_compute import ExtendComputeRequest, RenewalPolicy
from cloud_agent.workspace_broker_client import WorkspaceBrokerError

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
    "machine_identity_changed": "The verified machine identity has changed.",
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
}


# How long past its recorded deadline a missing resource is taken as ended.
_EXPIRY_GRACE = timedelta(minutes=10)
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

    async def schedule(self) -> None:
        """Observe leases and submit renewals even while retirement is blocked."""
        now = _now()
        for row in await self.repository.list_allocations():
            if row.get("release_committed_at") or row.get("observed_state") in {
                "terminated",
                "absent",
            }:
                continue
            operations = await self.repository.list_operations(allocation_id=row["id"])
            if row.get("external_id"):
                observed = _moment(row.get("observed_at"))
                due = (
                    observed is None
                    or (now - observed).total_seconds()
                    >= self.commands.configuration.observation_seconds
                )
                active_inspect = any(
                    op["kind"] in {"inspect", "acquire", "import"}
                    and op["state"] in {"pending", "running", "waiting", "unknown"}
                    for op in operations
                )
                if due and not active_inspect:
                    slot = int(now.timestamp()) // self.commands.configuration.observation_seconds
                    await self.commands.queue_inspection(
                        row, request_key=f"inspect:{row['id']}:{slot}"
                    )
            policy = RenewalPolicy.model_validate(row.get("policy") or {})
            expires = _moment(row.get("expires_at"))
            if not policy.enabled or expires is None or not row.get("expiration_known"):
                continue
            if row["management_mode"] != "manage" or expires - now > timedelta(
                seconds=policy.renew_before_seconds
            ):
                continue
            lifecycle = await self.repository.list_operations(
                allocation_id=row["id"], kinds=["extend", "rotate"]
            )
            trigger = self.rotation.due(row, policy, lifecycle, now, expires)
            if trigger is not None:
                await self.rotation.start(row, trigger, expires)
            await self.rotation.remind(row, lifecycle, now, expires)
            stalled = any(
                op["kind"] == "rotate" and op["state"] in {"blocked", "failed"} for op in lifecycle
            )
            if self.rotation.replaces_renewal(row, policy, now) and not stalled:
                # A node at its maximum age is replaced rather than extended,
                # unless its replacement is stuck: then time still helps.
                continue
            request_id = f"renew:{row['id']}:{int(expires.timestamp())}:{row['revision']}"
            if any(op["request_id"] == request_id for op in operations):
                continue
            request = ExtendComputeRequest(
                request_id=request_id,
                target_expires_at=now + timedelta(seconds=policy.extend_to_horizon_seconds),
                reason=policy.reason,
            )
            try:
                await self.commands.extend(row["id"], request, actor="compute-renewal")
            except (ComputeRefusal, ValueError):
                # Concurrent schedulers may have already persisted a fixed target.
                logger.warning("compute_renewal_not_submitted", extra={"allocation_id": row["id"]})

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
            else:
                raise ProviderError("unsupported_operation", "Unknown compute operation")
        except (ProviderError, ComputeRefusal, ComputeSecretError) as exc:
            code = getattr(exc, "code", "credential_unavailable")
            retryable = bool(getattr(exc, "retryable", False))
            if operation.get("allocation_id") and operation["kind"] != "rotate":
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
                await self._finish(
                    operation,
                    "waiting" if retryable else "blocked",
                    operation["step"],
                    str(exc),
                    result={"blocked_reasons": [{"code": code, "message": str(exc)}]},
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
            await self._finish(
                operation,
                "blocked",
                operation["step"],
                "Resource state changed; inspect the operation before retrying.",
                result={
                    "blocked_reasons": [
                        {
                            "code": "resource_state_conflict",
                            "message": "Resource identity or state no longer matches this operation.",
                        }
                    ]
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
        broker = broker_for_host(host)
        if broker is None:
            raise ProviderError(
                "broker_unreachable",
                "The control plane cannot reach the Host broker",
                retryable=True,
            )
        try:
            await broker.health()
        except WorkspaceBrokerError:
            raise ProviderError(
                "broker_unreachable",
                "The control plane cannot reach the Host broker",
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
        url = self.commands.configuration.registration_url
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
            if registration is None or (
                not registration.get("registered_at") and registration.get("expired")
            ):
                raise ProviderError(
                    "registration_expired",
                    "The node did not register its SSH host keys in time",
                )
            if not registration.get("registered_at"):
                await self._finish(
                    operation,
                    "waiting",
                    "waiting_for_registration",
                    "Waiting for the node to register its SSH host keys.",
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
        request = self._request(allocation)
        if self._registers_host_keys(allocation):
            request = request.model_copy(
                update={"registration": await self._open_registration(operation, allocation)}
            )
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
        if (
            observation.expires_at is not None
            and observation.expires_at >= request.target_expires_at
        ):
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
            else:
                await self._finish(
                    operation,
                    "waiting",
                    "confirming_extension",
                    "Waiting to confirm the previous extension request.",
                    result={"external_pending": True},
                    retry=30,
                )
            return
        await self._checkpoint(
            operation,
            "extending",
            {
                "extension_requested": True,
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
        if observed.expires_at is not None and observed.expires_at >= request.target_expires_at:
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

    async def _retire(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        if not allocation.get("release_committed_at"):
            self.commands._require_managed(allocation, "release")
            await self.repository.begin_retirement(
                allocation_id=allocation["id"], **self._fence(operation)
            )
            allocation = await self._allocation(allocation["id"])
            await self._checkpoint(operation, "checking_retention", {"external_pending": False})
            if allocation.get("external_id"):
                await self._inspect(operation, allocation)
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

    async def _verify_empty_inventory(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> None:
        if not allocation.get("deployment_profile"):
            raise ProviderError(
                "inventory_unavailable",
                "Attach a deployment profile and verified SSH connection before releasing this resource",
            )
        profile = DeploymentProfile.model_validate(allocation["deployment_profile"])
        for host in allocation.get("hosts", []):
            broker = broker_for_host(host)
            if broker is None:
                continue  # A trusted full-node scan below must prove this host empty.
            try:
                if await broker.list_sandboxes():
                    raise ProviderError(
                        "retained_groups_present", "The broker still reports retained sandbox state"
                    )
            except WorkspaceBrokerError:
                continue  # Never infer emptiness; require the full-node scan below.
        for node in allocation["nodes"]:
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
