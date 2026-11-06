"""Acquire a server, register its node and deploy its Host: the acquire, import and preflight steps.

Split from :mod:`cloud_agent.application.compute_reconcile`, which claims each
operation and dispatches it here. Every provider mutation is preceded by a
checkpoint, and a recovered acquisition is queried, never blindly recreated.
A node that registers its own SSH host keys is reached only once its key is
pinned.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from cloud_agent.application.compute_diagnostic_values import (
    cloudlab_observation_diagnostics,
    provider_receipt_diagnostic_result,
)
from cloud_agent.application.host_commands import HostCommands
from cloud_agent.domain.host_pool import hash_secret
from cloud_agent.ports.errors import ConflictCode, StorageConflict
from cloud_agent.ports.external.compute_provider import (
    AcquireRequest,
    NodeObservation,
    NodeRegistration,
    ProviderError,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
)
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.providers.compute import compute_config
from cloud_agent.providers.compute.compute_secrets import ComputeSecretError
from cloud_agent.sandbox_manager.sandbox_manager_client import SandboxManagerError
from cloud_agent.sandbox_manager.sandbox_manager_routing import sandbox_manager_for_host

if TYPE_CHECKING:
    from cloud_agent.application.compute_reconcile import ComputeReconciler

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
    return datetime.now(UTC)


def _provider_observation_is_fresh(observation: ResourceObservation) -> bool:
    """Reject stale provider snapshots before using them to authorize bootstrap."""
    observed_at = observation.observed_at
    if observed_at.tzinfo is None:
        return False
    age = (_now() - observed_at.astimezone(UTC)).total_seconds()
    return -30 <= age <= 300


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def registers_host_keys(allocation: dict[str, Any]) -> bool:
    """Whether this allocation's node supplies its own SSH host keys at first boot."""
    return (allocation.get("configuration") or {}).get("host_key_source") == "registration"


class ComputeAcquisition:
    """The acquire, import and preflight workflows, executed by the compute reconciler."""

    def __init__(self, reconciler: ComputeReconciler) -> None:
        self.reconciler = reconciler
        self.commands = reconciler.commands
        self.repository = reconciler.repository
        self.host_pool = reconciler.host_pool
        self.bootstrap = reconciler.bootstrap

    async def preflight(self, operation: dict[str, Any]) -> None:
        """Check a provider connection, or an attachable node, before anything is registered."""
        parameters = operation["parameters"]
        entry = self.commands.registry.get(parameters["provider_id"])
        config = parameters["validated_configuration"]
        await self.reconciler._checkpoint(operation, "checking_configuration")
        if parameters["mode"] == "provision":
            await entry.validate_connection(config["credential_ref"], {})
            await self.reconciler._finish(
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
        intent = self.reconciler._intent(
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
            await self.reconciler._bootstrap_blocked(operation, checked.blocked_reasons)
            return
        await self.reconciler._finish(
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

    async def acquire(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:  # noqa: C901, PLR0912, PLR0915 complexity baseline, #771
        """Acquire or import the resource, then deploy and verify its Host."""
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        observation: ResourceObservation | None
        if allocation.get("external_id"):
            observation = await provider.inspect(self.reconciler._ref(allocation))
        elif operation.get("result", {}).get("resource_ref"):
            observation = await provider.inspect(
                ResourceRef.model_validate(operation["result"]["resource_ref"])
            )
        elif operation.get("step") != "pending":
            resolution = await provider.recover_acquire(
                self.reconciler._intent(operation, allocation)
            )
            if resolution.outcome == "not_created":
                await self.reconciler._checkpoint(operation, "acquiring")
                receipt = await self.submit_acquisition(operation, allocation)
            elif resolution.receipt is not None:
                receipt = resolution.receipt
            else:
                raise ProviderError(
                    "result_unknown", "The provider cannot identify the previous acquisition"
                )
            observation = await self.receipt_observation(operation, allocation, receipt)
        else:
            await self.reconciler._checkpoint(operation, "acquiring")
            receipt = await self.submit_acquisition(operation, allocation)
            observation = await self.receipt_observation(operation, allocation, receipt)
        if observation is None:
            raise ProviderError(
                "result_unknown", "The provider did not return an identifiable resource"
            )
        await self.reconciler._save_observation(operation, allocation, observation)
        if observation.state == "terminated":
            raise ProviderError("resource_terminated", "The allocated resource has terminated")
        if observation.state == "failed":
            await self.release_failed_acquisition(operation, allocation)
            diagnostics = (
                cloudlab_observation_diagnostics(
                    observation, reason_code="experiment_failed_no_provider_reason"
                )
                if allocation.get("provider_id") == "cloudlab"
                else {}
            )
            raise ProviderError(
                "resource_failed",
                "The provider could not set up this resource; it was released",
                diagnostics=diagnostics,
            )
        if observation.state != "ready":
            await self.reconciler._finish(
                operation,
                "waiting",
                "waiting_for_resource",
                "Waiting for the provider to make the resource ready.",
                result={"external_pending": True},
                retry=30,
            )
            return
        if operation["kind"] == "import":
            await self.reconciler._checkpoint(
                operation, "resource_ready", {"external_pending": False}
            )
            await self.reconciler._finish(
                operation,
                "succeeded",
                "observed",
                "Resource imported for observation; no Host was deployed.",
            )
            return
        if not observation.scope_complete:
            diagnostics = (
                cloudlab_observation_diagnostics(
                    observation, reason_code="incomplete_resource_scope"
                )
                if allocation.get("provider_id") == "cloudlab"
                else {}
            )
            raise ProviderError(
                "resource_scope_incomplete",
                "The provider has not reported the complete resource inventory.",
                diagnostics=diagnostics,
            )
        await self.reconciler._checkpoint(operation, "resource_ready", {"external_pending": False})
        if len(observation.nodes) != 1:
            diagnostics = (
                cloudlab_observation_diagnostics(observation, reason_code="unsupported_node_count")
                if allocation.get("provider_id") == "cloudlab"
                else {}
            )
            raise ProviderError(
                "unsupported_node_count",
                "This deployment profile supports one node; the full allocation remains visible for review",
                diagnostics=diagnostics,
            )
        allocation = await self.reconciler._allocation(allocation["id"])
        # The original ready observation only admitted node registration. Read
        # CloudLab again immediately before creating the Host binding, then
        # persist that full snapshot so the repository can guard the commit
        # against current allocation state under its compute-entry lock.
        latest = await provider.inspect(self.reconciler._ref(allocation))
        if not _provider_observation_is_fresh(latest):
            await self.reconciler._finish(
                operation,
                "waiting",
                "waiting_for_resource",
                "Waiting for a fresh provider observation before bootstrapping the host.",
                result={"external_pending": True},
                retry=30,
            )
            return
        await self.reconciler._save_observation(operation, allocation, latest)
        if latest.state == "terminated":
            raise ProviderError("resource_terminated", "The allocated resource has terminated")
        if latest.state == "failed":
            await self.release_failed_acquisition(operation, allocation)
            diagnostics = (
                cloudlab_observation_diagnostics(
                    latest, reason_code="experiment_failed_no_provider_reason"
                )
                if allocation.get("provider_id") == "cloudlab"
                else {}
            )
            raise ProviderError(
                "resource_failed",
                "The provider could not set up this resource; it was released",
                diagnostics=diagnostics,
            )
        if latest.state != "ready" or not latest.scope_complete:
            await self.reconciler._finish(
                operation,
                "waiting",
                "waiting_for_resource",
                "Waiting for the provider to report one complete ready node before bootstrapping.",
                result={"external_pending": True},
                retry=30,
            )
            return
        if len(latest.nodes) != 1:
            diagnostics = (
                cloudlab_observation_diagnostics(latest, reason_code="unsupported_node_count")
                if allocation.get("provider_id") == "cloudlab"
                else {}
            )
            raise ProviderError(
                "unsupported_node_count",
                "This deployment profile supports one node; the full allocation remains visible for review",
                diagnostics=diagnostics,
            )
        allocation = await self.reconciler._allocation(allocation["id"])
        if (
            not allocation.get("scope_complete")
            or allocation.get("observed_state") not in {"ready", "active", "running"}
            or allocation.get("observation_error") is not None
            or len(allocation.get("nodes", [])) != 1
            or allocation.get("observed_at") is None
            or (_now() - allocation["observed_at"]).total_seconds() > 300
        ):
            await self.reconciler._finish(
                operation,
                "waiting",
                "waiting_for_resource",
                "Waiting for the current allocation snapshot to become complete and ready.",
                result={"external_pending": True},
                retry=30,
            )
            return
        observation = latest
        node = allocation["nodes"][0]
        registered = await self.registered_node(operation, allocation, node, latest.nodes[0])
        if registered is None:
            return
        observed_node = registered
        profile = DeploymentProfile.model_validate(allocation["deployment_profile"])
        intent = self.reconciler._bootstrap_intent(operation, allocation, node)
        if operation.get("parameters", {}).get("rotation_operation_id"):
            # A replacement node: staged and filled with a copy, never started here.
            await self.reconciler.rotation.stage_candidate(
                operation, allocation, observed_node, profile, intent
            )
            return
        await self.repository.prepare_bootstrap_binding(
            node_id=node["id"], host=intent.host, **self.reconciler._fence(operation)
        )
        host = await self.host_pool.get_host(host=intent.host)
        if not operation.get("result", {}).get("deployed"):
            await self.reconciler._checkpoint(operation, "deploying")
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
                await self.reconciler._bootstrap_blocked(
                    operation,
                    deployed.blocked_reasons,
                    observation=observation,
                    provider_id=allocation.get("provider_id"),
                )
                return
            await self.reconciler._checkpoint(operation, "verifying_host", {"deployed": True})
        readiness = await self.bootstrap.readiness(observed_node, profile, intent)
        if not readiness.ready:
            await self.reconciler._bootstrap_blocked(
                operation,
                readiness.blocked_reasons,
                observation=observation,
                provider_id=allocation.get("provider_id"),
            )
            return
        host = await self.host_pool.get_host(host=intent.host)
        if host is None or host.get("status") != "online" or not host.get("enrolled"):
            await self.reconciler._finish(
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
            node_id=node["id"], host=intent.host, **self.reconciler._fence(operation)
        ):
            raise ValueError("bootstrap_binding_changed")
        await self.reconciler._finish(
            operation,
            "succeeded",
            "ready",
            "Server is ready. It can now be selected as the global sandbox location.",
            result={"nodes_ready": 1, "external_pending": False, "blocked_reasons": []},
        )

    async def open_registration(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> NodeRegistration:
        """Mint the one-time token this acquisition's node registers its host keys with.

        Each submission gets a fresh token; only the newest one of an
        allocation can register, and only its hash is stored.
        """
        url = compute_config.current_registration_url(self.commands.configuration)
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
            **self.reconciler._fence(operation),
        )
        return NodeRegistration(url=url, token=SecretStr(token))

    async def registered_node(
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
        if connection is None or not registers_host_keys(allocation):
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
                    **self.reconciler._fence(operation),
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
                await self.reconciler._finish(
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
                node_id=node["id"], host_key=pinned, **self.reconciler._fence(operation)
            )
        return observed.model_copy(
            update={"connection": connection.model_copy(update={"host_key": pinned})}
        )

    async def release_failed_acquisition(
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
        allocation = await self.reconciler._allocation(allocation["id"])
        await self.reconciler._checkpoint(operation, "releasing_failed", {"external_pending": True})
        receipt = await provider.release(
            self.reconciler._ref(allocation), self.reconciler._intent(operation, allocation)
        )
        observation = receipt.observation or await provider.inspect(
            self.reconciler._ref(allocation)
        )
        await self.reconciler._save_observation(operation, allocation, observation)
        if observation.state != "terminated":
            raise ProviderError(
                "release_pending",
                "Waiting for the provider to remove the failed resource",
                retryable=True,
            )
        await self.reconciler._checkpoint(
            operation, "failed_resource_released", {"external_pending": False}
        )

    async def submit_acquisition(
        self, operation: dict[str, Any], allocation: dict[str, Any]
    ) -> ProviderReceipt:
        """Submit the acquisition once; a definite refusal resets the step for a retry."""
        provider = self.commands.registry.get(allocation["provider_id"]).provider
        try:
            request = self.reconciler._request(allocation)
            if registers_host_keys(allocation):
                request = request.model_copy(
                    update={"registration": await self.open_registration(operation, allocation)}
                )
            await self.repository.assert_autoscale_acquire_connections_verified(
                allocation_id=allocation["id"], **self.reconciler._fence(operation)
            )
        except (ProviderError, ComputeSecretError, ValueError) as exc:
            if isinstance(exc, StorageConflict) and exc.code is ConflictCode.OPERATION_LEASE_LOST:
                raise
            # Nothing has reached the provider yet, so every failure here is
            # a definite refusal and can safely release an autoscaling slot.
            await self.reconciler._checkpoint(operation, "pending", {"external_pending": False})
            raise
        try:
            return await provider.acquire(request, self.reconciler._intent(operation, allocation))
        except (ProviderError, ComputeSecretError) as exc:
            if isinstance(exc, ProviderError) and exc.diagnostics:
                # A definite rejection resets the acquisition step below so
                # it can be retried after repair. Preserve where this attempt
                # failed before that retry checkpoint changes the step.
                exc.diagnostics["_failure_stage"] = str(operation.get("step") or "unknown")
            if getattr(exc, "code", "credential_unavailable") != "result_unknown":
                # A definite rejection is safe to retry after credentials or
                # capacity are repaired. Ambiguous submissions keep the fence.
                await self.reconciler._checkpoint(operation, "pending", {"external_pending": False})
            raise

    async def receipt_observation(
        self, operation: dict[str, Any], allocation: dict[str, Any], receipt: ProviderReceipt
    ) -> ResourceObservation | None:
        """Checkpoint the identity a receipt names, then return what the provider shows of it."""
        if receipt.resource is not None:
            await self.reconciler._checkpoint(
                operation,
                "resource_identified",
                {
                    "resource_ref": receipt.resource.model_dump(mode="json"),
                    "external_id": receipt.resource.external_id,
                    **provider_receipt_diagnostic_result(receipt, operation),
                },
            )
        if receipt.observation is not None:
            return receipt.observation
        if receipt.resource is not None:
            provider = self.commands.registry.get(allocation["provider_id"]).provider
            return await provider.inspect(receipt.resource)
        if receipt.outcome == "unknown" and receipt.diagnostics:
            raise ProviderError(
                "result_unknown",
                "The provider did not return an identifiable resource",
                diagnostics=receipt.diagnostics,
            )
        return None
