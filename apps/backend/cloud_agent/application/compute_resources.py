"""Administrator commands over compute resources; external work is queued.

The HTTP router authenticates and audits. This layer validates capabilities,
credential ownership, fixed request identity, and management scope. It never
runs provider mutations inline with an HTTP request.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from cloud_agent.application.cloudlab_inventory import (
    CloudLabInventoryUnavailable,
    CloudLabNodeInventory,
)
from cloud_agent.application.server_queries import ServerQueries
from cloud_agent.compute_config import current_observation_seconds
from cloud_agent.domain.compute import (
    ComputeRefusal,
    autoscaling_acquire_can_cancel,
    autoscaling_operation_is_terminal,
    autoscaling_retirement_needs_settlement,
    fingerprint,
    normalize_host_keys,
    public_connection,
    public_operation,
)
from cloud_agent.host_pool import hash_secret
from cloud_agent.ports.external.host_bootstrap import DeploymentProfile
from cloud_agent.schemas_compute import CloudLabAutoscalingPolicy, CloudLabDefaults, RenewalPolicy

if TYPE_CHECKING:
    from cloud_agent.application.compute_setup import AutomaticComputeSetup
    from cloud_agent.compute_config import ComputeConfiguration
    from cloud_agent.compute_registry import ComputeProviderRegistration, ComputeProviderRegistry
    from cloud_agent.compute_secrets import ComputeSecretStore
    from cloud_agent.ports.compute_resources import ComputeResourceRepository
    from cloud_agent.schemas_compute import (
        AddComputeConnection,
        AddDeploymentProfile,
        AddServerRequest,
        AutomaticDeploymentSetup,
        ExtendComputeRequest,
        ImportComputeRequest,
        RetireComputeRequest,
        RotateComputeRequest,
        UpdateCloudLabAutoscaling,
        UpdateComputeConnection,
        UpdateComputePolicy,
    )


class ComputeCommands:
    """One application surface for UI commands and automatic renewal intents."""

    def __init__(
        self,
        *,
        repository: ComputeResourceRepository,
        registry: ComputeProviderRegistry,
        secrets: ComputeSecretStore,
        configuration: ComputeConfiguration,
        automatic_setup: AutomaticComputeSetup | None = None,
        cloudlab_inventory: CloudLabNodeInventory | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.secrets = secrets
        self.configuration = configuration
        self.automatic_setup = automatic_setup
        self.cloudlab_inventory = cloudlab_inventory
        self.queries = ServerQueries(
            repository,
            registry,
            secrets,
            observation_seconds=lambda: current_observation_seconds(configuration),
        )

    async def catalog(self) -> dict[str, Any]:
        """Return enabled implementations and public profile names only."""
        profiles = await self.deployment_profiles()
        providers = []
        for entry in self.registry.entries.values():
            defaults = await self.cloudlab_defaults() if entry.id == "cloudlab" else {}
            providers.append(entry.catalog(defaults))
        return {
            "providers": providers,
            "deployment_profiles": [{"id": p.id, "label": p.label} for p in profiles.values()],
            "secret_store_configured": self.secrets.configured,
        }

    async def deployment_profiles(self) -> dict[str, DeploymentProfile]:
        """Merge legacy operator files with profiles registered through Admin."""
        profiles = dict(self.configuration.profiles)
        listing = getattr(self.repository, "list_deployment_profiles", None)
        if listing is None:
            return profiles
        for row in await listing():
            if not row["configuration"].get("automatic_setup", {}).get("ready", True):
                continue
            profile = self._stored_profile(row)
            profiles[profile.id] = profile
        return profiles

    async def automatic_setup_status(self) -> dict[str, Any]:
        """Describe automatic setup without returning deployment credentials."""
        if self.automatic_setup is None:
            return {
                "available": False,
                "deployment_sha": None,
                "blocked_reasons": [
                    {
                        "code": "automatic_setup_unavailable",
                        "message": "Automatic setup is unavailable in this deployment. Update the control plane or use advanced manual setup.",
                    }
                ],
            }
        return await self.automatic_setup.read_status()

    async def create_automatic_setup(self, request: AutomaticDeploymentSetup) -> dict[str, Any]:
        """Prepare reviewed release inputs and narrowly scoped infrastructure access."""
        if self.automatic_setup is None:
            raise ComputeRefusal(
                "automatic_setup_unavailable",
                "Automatic setup is unavailable in this deployment.",
                status=503,
            )
        return await self.automatic_setup.create(request)

    async def create_deployment_profile(self, request: AddDeploymentProfile) -> dict[str, Any]:
        """Validate, encrypt and persist one write-only Host setup."""
        try:
            bundle = request.bundle()
        except ValueError:
            raise ComputeRefusal(
                "invalid_configuration", "The release bundle is invalid.", status=422
            ) from None
        self._validate_bundle(bundle)
        files = {name: value.get_secret_value() for name, value in request.files.items()}
        if any(
            not name.startswith(".compute-secrets/")
            or name.endswith("/")
            or ".." in name.split("/")
            for name in files
        ):
            raise ComputeRefusal(
                "invalid_configuration",
                "Deployment credential files must be inside .compute-secrets/.",
                status=422,
            )
        digest = hashlib.sha256(bundle).hexdigest()
        configuration = {
            "deployment_sha": request.deployment_sha,
            "deploy_root": request.deploy_root,
            "files": list(files),
            "install_prerequisites": request.install_prerequisites,
            "timeout_seconds": request.timeout_seconds,
            "data_mount": request.data_mount,
        }
        self._stored_profile(
            {
                "id": request.id,
                "label": request.label,
                "source_sha256": digest,
                "configuration": configuration,
            }
        )
        plaintext = json.dumps(
            {"environment": request.environment.get_secret_value(), "files": files},
            separators=(",", ":"),
        )
        row = await self.repository.save_deployment_profile(
            profile_id=request.id,
            label=request.label,
            configuration=configuration,
            source_bundle=bundle,
            source_sha256=digest,
            secret_ciphertext=self.secrets.encrypt(
                "profile:" + request.id, "deployment_profile", plaintext
            ),
        )
        return {"id": row["id"], "label": row["label"]}

    @staticmethod
    def _validate_bundle(bundle: bytes) -> None:
        """Reject unsafe or incomplete archives before they become selectable."""
        from cloud_agent.application.compute_setup import validate_host_bundle

        try:
            validate_host_bundle(bundle)
        except ValueError as exc:
            if str(exc) == "incomplete":
                raise ComputeRefusal(
                    "invalid_configuration",
                    "The release bundle is missing the Host recipe.",
                    status=422,
                ) from None
            raise ComputeRefusal(
                "invalid_configuration", "The release bundle is invalid or unsafe.", status=422
            ) from None

    @staticmethod
    def _stored_profile(row: dict[str, Any]) -> DeploymentProfile:
        config = row["configuration"]
        profile_id = row["id"]
        return DeploymentProfile(
            id=profile_id,
            label=row["label"],
            source_bundle=f"stored-profile:{profile_id}:bundle",
            source_sha256=row["source_sha256"],
            deployment_sha=config["deployment_sha"],
            deploy_root=config["deploy_root"],
            environment_ref=f"stored-profile:{profile_id}:environment",
            files={
                name: f"stored-profile:{profile_id}:file:{name}" for name in config.get("files", [])
            },
            install_prerequisites=config.get("install_prerequisites", True),
            timeout_seconds=config.get("timeout_seconds", 3600),
            data_mount=config.get("data_mount"),
        )

    async def cloudlab_defaults(self) -> dict[str, Any]:
        """Return validated effective defaults for the configured CloudLab adapter."""
        self.registry.get("cloudlab")
        stored = await self.repository.provider_defaults("cloudlab")
        return CloudLabDefaults.model_validate(stored).model_dump(mode="json")

    async def set_cloudlab_defaults(self, defaults: CloudLabDefaults) -> dict[str, Any]:
        """Replace the non-secret defaults used for new CloudLab requests."""
        self.registry.get("cloudlab")
        return await self.repository.set_provider_defaults(
            "cloudlab", defaults.model_dump(mode="json")
        )

    @staticmethod
    def _cloudlab_autoscaling_view(snapshot: dict[str, Any]) -> dict[str, Any]:
        raw_policy = dict(snapshot["policy"])
        revision = int(raw_policy.pop("revision", 0))
        fields = CloudLabAutoscalingPolicy.model_fields
        policy = CloudLabAutoscalingPolicy.model_validate(
            {key: value for key, value in raw_policy.items() if key in fields}
        ).model_dump(mode="json")
        policy["revision"] = revision
        return {**snapshot, "policy": policy}

    async def cloudlab_autoscaling(self) -> dict[str, Any]:
        """Return the safe singleton policy and its durable controller status."""
        return self._cloudlab_autoscaling_view(await self.repository.get_cloudlab_autoscaling())

    async def cloudlab_preferred_nodes(self) -> dict[str, Any]:
        """Return fresh public availability for the conservative node shortlist."""
        if self.cloudlab_inventory is None:
            raise ComputeRefusal(
                "cloudlab_availability_unavailable",
                "CloudLab node availability is temporarily unavailable.",
                status=503,
            )
        try:
            return await self.cloudlab_inventory.preferred_nodes()
        except CloudLabInventoryUnavailable:
            raise ComputeRefusal(
                "cloudlab_availability_unavailable",
                "CloudLab node availability is temporarily unavailable.",
                status=503,
            ) from None

    async def set_cloudlab_autoscaling(
        self, request: UpdateCloudLabAutoscaling, *, actor: str
    ) -> dict[str, Any]:
        """Validate CloudLab authority, immutable routes and automatic placement."""
        policy = request.policy
        requested_policy = policy.model_dump(mode="json")
        current_policy = self._cloudlab_autoscaling_view(
            await self.repository.get_cloudlab_autoscaling()
        )["policy"]
        pause_existing = bool(
            current_policy.get("enabled")
            and not policy.enabled
            and all(
                current_policy.get(key) == value
                for key, value in requested_policy.items()
                if key != "enabled"
            )
        )
        configured = bool(
            policy.connection_id
            or policy.ssh_connection_id
            or policy.configuration
            or policy.deployment_profiles
            or policy.max_hosts
        )
        canonical_configuration = dict(policy.configuration)
        if configured and not pause_existing:
            if policy.connection_id is None or policy.ssh_connection_id is None:
                raise ComputeRefusal(
                    "autoscaling_connections_required",
                    "Select verified CloudLab and SSH connections.",
                    status=422,
                )
            cloudlab = await self._connection(policy.connection_id, "cloudlab")
            ssh = await self._connection(policy.ssh_connection_id, "existing_ssh")
            if cloudlab.get("status") != "verified" or ssh.get("status") != "verified":
                raise ComputeRefusal(
                    "autoscaling_connection_unverified",
                    "Validate both connections before saving autoscaling.",
                )
            entry, validated = await self._configuration(
                "cloudlab",
                "provision",
                policy.connection_id,
                policy.ssh_connection_id,
                policy.configuration,
                deploy=True,
            )
            if validated.get("host_key_source") != "registration":
                raise ComputeRefusal(
                    "autoscaling_registration_required",
                    "Autoscaled CloudLab nodes must register and pin their host keys.",
                    status=422,
                )
            if "name" in policy.configuration or "external_id" in policy.configuration:
                raise ComputeRefusal(
                    "invalid_configuration",
                    "Autoscaling generates a unique experiment identity for every node.",
                    status=422,
                )
            allowed = {field["key"] for field in entry.fields}
            canonical_configuration = {
                key: value for key, value in validated.items() if key in allowed
            }
        profiles = {} if pause_existing else await self.deployment_profiles()
        missing = (
            []
            if pause_existing
            else [
                profile_id
                for profile_id in policy.deployment_profiles
                if profile_id not in profiles
            ]
        )
        if missing:
            raise ComputeRefusal(
                "autoscaling_profile_unavailable",
                "Every autoscaling deployment profile must be configured and ready.",
                status=422,
            )
        normalized = policy.model_copy(
            update={"configuration": canonical_configuration}
        ).model_dump(mode="json")
        snapshots = (
            {}
            if pause_existing
            else {
                profile_id: profiles[profile_id].model_dump(mode="json")
                for profile_id in policy.deployment_profiles
            }
        )
        saved = await self.repository.update_cloudlab_autoscaling(
            expected_revision=request.expected_revision,
            policy=normalized,
            profile_snapshots=snapshots,
            requested_by=actor,
        )
        return self._cloudlab_autoscaling_view(saved)

    async def connections(self) -> dict[str, Any]:
        """List metadata with a second whitelist above the storage boundary."""
        return {
            "connections": [public_connection(r) for r in await self.repository.list_connections()]
        }

    async def create_connection(self, request: AddComputeConnection) -> dict[str, Any]:
        """Persist an encrypted credential under a newly allocated reference."""
        entry = self.registry.get(request.provider_id)
        secret = request.secret.get_secret_value()
        configuration = entry.validate_secret(request.configuration, secret)
        connection_id = str(uuid4())
        row = await self.repository.save_connection(
            connection_id=connection_id,
            name=request.name,
            provider_id=entry.id,
            configuration=configuration,
            secret_ciphertext=self.secrets.encrypt(connection_id, entry.id, secret),
            masked_summary=f"{entry.secret_label} configured",
            status="unverified",
        )
        return public_connection(row)

    async def update_connection(
        self, connection_id: str, request: UpdateComputeConnection
    ) -> dict[str, Any]:
        """Rotate in place without returning credentials or trusting stale editors."""
        old = await self._connection(connection_id)
        entry = self.registry.get(old["provider_id"])
        secret = (
            request.secret.get_secret_value()
            if request.secret is not None
            else self.secrets.decrypt(connection_id, entry.id, old["secret_ciphertext"])
        )
        configuration = entry.validate_secret(
            old["configuration"] if request.configuration is None else request.configuration, secret
        )
        row = await self.repository.save_connection(
            connection_id=connection_id,
            name=request.name or old["name"],
            provider_id=entry.id,
            configuration=configuration,
            secret_ciphertext=self.secrets.encrypt(connection_id, entry.id, secret),
            masked_summary=f"{entry.secret_label} configured",
            status="unverified",
            expected_revision=request.expected_revision,
        )
        # A saved credential does not inherit the health of the previous one.
        for allocation in await self.repository.list_allocations():
            if (
                connection_id
                in (allocation.get("connection_id"), allocation.get("ssh_connection_id"))
                and allocation.get("external_id")
                and not allocation.get("release_committed_at")
            ):
                try:
                    await self.queue_inspection(
                        allocation,
                        request_key=f"connection:{connection_id}:{row['revision']}:{allocation['id']}",
                    )
                except ValueError as exc:
                    if str(exc) not in {"release_committed", "allocation_not_found"}:
                        raise
        return public_connection(row)

    async def validate_connection(self, connection_id: str) -> dict[str, Any]:
        """Check credential material; cloud adapters may additionally verify access."""
        row = await self._connection(connection_id)
        entry = self.registry.get(row["provider_id"])
        secret = self.secrets.decrypt(connection_id, entry.id, row["secret_ciphertext"])
        entry.validate_secret(row["configuration"], secret)
        # The validator is declared by the registration, not inferred from
        # adapter names or overloaded acquisition requests.
        await entry.validate_connection(connection_id, row["configuration"])
        saved = await self.repository.save_connection(
            connection_id=connection_id,
            name=row["name"],
            provider_id=entry.id,
            configuration=row["configuration"],
            secret_ciphertext=row["secret_ciphertext"],
            masked_summary=row["masked_summary"],
            status="verified",
            expected_revision=row["revision"],
        )
        return public_connection(saved)

    async def add_server(
        self, request: AddServerRequest, *, actor: str, preflight: bool = False
    ) -> dict[str, Any]:
        """Record one attachment or acquisition before any external side effect."""
        kind = "preflight" if preflight else "acquire"
        parameters = request.model_dump(mode="json")
        if request.sandbox_storage_path is None:
            # Preserve fingerprints of requests recorded before this optional
            # creation field existed.
            parameters.pop("sandbox_storage_path")
        existing = await self._existing(request.request_id, kind, parameters)
        if existing is not None:
            return existing
        entry, config = await self._configuration(
            request.provider_id,
            request.mode,
            request.connection_id,
            request.ssh_connection_id,
            request.configuration,
            deploy=True,
        )
        try:
            profile = await self._deployment_profile(request.deployment_profile)
        except ComputeRefusal:
            # A duplicate request may have claimed the last free profile after
            # our first lookup. Recover that operation before refusing capacity.
            existing = await self._existing(request.request_id, kind, parameters)
            if existing is not None:
                return existing
            raise
        if request.sandbox_storage_path is not None:
            profile = profile.model_copy(
                update={"sandbox_storage_path": request.sandbox_storage_path}
            )
        if request.policy.enabled and (
            not entry.provider.capabilities().can_extend or request.mode != "provision"
        ):
            raise ComputeRefusal(
                "renewal_not_managed",
                "Enable renewal after explicitly adopting the allocation's management scope.",
            )
        if preflight:
            # No inventory is adopted and no cloud acquire is called by this operation.
            operation = await self.repository.create_operation(
                request_id=request.request_id,
                fingerprint=fingerprint(kind, parameters),
                kind=kind,
                allocation_id=None,
                parameters={
                    **parameters,
                    "validated_configuration": config,
                    "profile": profile.model_dump(mode="json"),
                },
                requested_by=actor,
            )
        else:
            operation = await self.repository.create_allocation_operation(
                request_id=request.request_id,
                fingerprint=fingerprint(kind, parameters),
                kind=kind,
                allocation={
                    "name": request.name,
                    "provider_id": entry.id,
                    "mode": request.mode,
                    "connection_id": request.connection_id,
                    "ssh_connection_id": request.ssh_connection_id,
                    "management_mode": "manage" if request.mode == "provision" else "observe",
                    "lease_managed": entry.provider.capabilities().can_extend,
                    "release_supported": entry.provider.capabilities().can_release,
                    "configuration": config,
                    "external_id": config.get("external_id"),
                    "deployment_profile": profile.model_dump(mode="json"),
                    "policy": request.policy.model_dump(mode="json"),
                },
                parameters=parameters,
                requested_by=actor,
            )
        return await self.operation_view(operation)

    async def import_resource(self, request: ImportComputeRequest, *, actor: str) -> dict[str, Any]:
        """Import observation only, with no SSH deployment or management authority."""
        parameters = request.model_dump(mode="json")
        existing = await self._existing(request.request_id, "import", parameters)
        if existing is not None:
            return existing
        entry, config = await self._configuration(
            request.provider_id,
            "attach",
            request.connection_id,
            None,
            request.configuration,
            deploy=False,
        )
        operation = await self.repository.create_allocation_operation(
            request_id=request.request_id,
            fingerprint=fingerprint("import", parameters),
            kind="import",
            allocation={
                "name": request.name,
                "provider_id": entry.id,
                "mode": "attach",
                "connection_id": request.connection_id,
                "configuration": config,
                "external_id": config.get("external_id"),
                "lease_managed": entry.provider.capabilities().can_extend,
                "release_supported": entry.provider.capabilities().can_release,
                "policy": RenewalPolicy().model_dump(mode="json"),
            },
            parameters=parameters,
            requested_by=actor,
        )
        return await self.operation_view(operation)

    async def update_policy(
        self, allocation_id: str, request: UpdateComputePolicy
    ) -> dict[str, Any]:
        """Save policy independently from the result of a future renewal."""
        row = await self._allocation(allocation_id)
        self._require_manual_policy_control(row)
        entry = self.registry.get(row["provider_id"])
        mode = request.management_mode or row["management_mode"]
        if request.management_mode == "manage" and not row.get("scope_complete"):
            raise ComputeRefusal(
                "unmanaged_scope", "The complete allocation scope has not been verified."
            )
        if request.policy.enabled:
            if not self.secrets.configured or not entry.provider.capabilities().can_extend:
                raise ComputeRefusal(
                    "renewal_unavailable", "This allocation cannot be automatically renewed."
                )
            if mode != "manage" or not row.get("scope_complete"):
                raise ComputeRefusal(
                    "unmanaged_scope",
                    "Review and adopt the complete allocation before enabling renewal.",
                )
        if request.policy.rotation_enabled:
            self._require_movable(row, entry)
        updated = await self.repository.update_policy(
            allocation_id=allocation_id,
            expected_revision=request.expected_revision,
            policy=request.policy.model_dump(mode="json"),
            management_mode=request.management_mode,
        )
        return self.allocation_view({**row, **updated})

    async def extend(
        self, allocation_id: str, request: ExtendComputeRequest, *, actor: str
    ) -> dict[str, Any]:
        """Submit one absolute target; retries do not extend relative to retry time."""
        parameters = {"allocation_id": allocation_id, **request.model_dump(mode="json")}
        existing = await self._existing(request.request_id, "extend", parameters)
        if existing is not None:
            return existing
        row = await self._allocation(allocation_id)
        self._require_managed(row, "extend")
        if request.target_expires_at <= datetime.now(timezone.utc):
            raise ComputeRefusal(
                "invalid_expiration", "The requested expiration must be in the future.", status=422
            )
        operation = await self.repository.create_operation(
            request_id=request.request_id,
            fingerprint=fingerprint("extend", parameters),
            kind="extend",
            allocation_id=allocation_id,
            parameters=parameters,
            requested_by=actor,
        )
        return await self.operation_view(operation)

    async def rotate(
        self, allocation_id: str, request: RotateComputeRequest, *, actor: str
    ) -> dict[str, Any]:
        """Queue a move of the server's Host to a replacement node now."""
        row = await self._allocation(allocation_id)
        self._require_manual_policy_control(row)
        parameters = {
            "allocation_id": allocation_id,
            **request.model_dump(mode="json"),
            "trigger": "manual",
        }
        existing = await self._existing(request.request_id, "rotate", parameters)
        if existing is not None:
            return existing
        self._require_managed(row, "release")
        self._require_movable(row, self.registry.get(row["provider_id"]))
        if len(row.get("hosts") or []) != 1 or len(row.get("nodes") or []) != 1:
            raise ComputeRefusal(
                "rotation_unsupported", "Only a server running exactly one Host can move."
            )
        if not RenewalPolicy.model_validate(row.get("policy") or {}).node_types:
            raise ComputeRefusal(
                "rotation_node_types_missing",
                "List the node types a replacement may use in the renewal policy first.",
                status=422,
            )
        if row["revision"] != request.expected_revision:
            raise ComputeRefusal(
                "revision_conflict", "The allocation changed; refresh it before moving its Host."
            )
        try:
            operation = await self.repository.create_operation(
                request_id=request.request_id,
                fingerprint=fingerprint("rotate", parameters),
                kind="rotate",
                allocation_id=allocation_id,
                parameters=parameters,
                requested_by=actor,
            )
        except ValueError as exc:
            if str(exc) == "rotation_in_progress":
                raise ComputeRefusal(
                    "rotation_in_progress", "This server is already moving to a replacement node."
                ) from None
            raise
        return await self.operation_view(operation)

    @staticmethod
    def _require_movable(row: dict[str, Any], entry: ComputeProviderRegistration) -> None:
        """A Host moves only between nodes this control plane creates and can trust unseen."""
        caps = entry.provider.capabilities()
        if (
            row.get("mode") != "provision"
            or "provision" not in caps.acquisition_modes
            or not caps.can_extend
            or not caps.can_release
            or not row.get("deployment_profile")
        ):
            raise ComputeRefusal(
                "rotation_unsupported",
                "Only a server this control plane provisioned, with a deployment profile, can move.",
            )
        if (row.get("configuration") or {}).get("host_key_source") != "registration":
            raise ComputeRefusal(
                "rotation_needs_registration",
                "Replacement nodes must register their own host keys; "
                "set SSH host-key checking to registration for this server.",
            )

    async def retire(
        self, allocation_id: str, request: RetireComputeRequest, *, actor: str
    ) -> dict[str, Any]:
        """Persist retirement intent; the worker rechecks all dependencies."""
        parameters = {"allocation_id": allocation_id, **request.model_dump(mode="json")}
        existing = await self._existing(request.request_id, "retire", parameters)
        if existing is not None:
            return existing
        row = await self._allocation(allocation_id)
        self._require_managed(row, "release")
        if row["revision"] != request.expected_revision:
            raise ComputeRefusal(
                "revision_conflict", "The allocation changed; refresh its impact before retrying."
            )
        operation = await self.repository.create_operation(
            request_id=request.request_id,
            fingerprint=fingerprint("retire", parameters),
            kind="retire",
            allocation_id=allocation_id,
            parameters=parameters,
            requested_by=actor,
        )
        return await self.operation_view(operation)

    async def register_node(self, token: str, host_keys: list[str]) -> None:
        """Record a new node's SSH host keys against its one-time token.

        Every refusal for an unknown, expired, spent or superseded token looks
        the same to the caller. Keys are compared as a set: the same keys again
        succeed, a different set never replaces the registered one.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            raise ComputeRefusal(
                "registration_not_found", "No open registration matches this token.", status=404
            )
        keys = normalize_host_keys(host_keys)
        outcome = await self.repository.complete_node_registration(
            token_hash=hash_secret(token), host_keys=keys
        )
        if outcome == "conflict":
            raise ComputeRefusal(
                "registration_conflict",
                "Different host keys are already registered for this node.",
                status=409,
            )
        if outcome not in {"registered", "replayed"}:
            raise ComputeRefusal(
                "registration_not_found", "No open registration matches this token.", status=404
            )

    async def servers(self) -> dict[str, Any]:
        """Read inventory through the independent query surface."""
        return await self.queries.servers()

    async def remove_server(self, allocation_id: str, *, expected_revision: int) -> None:
        """Remove local server records after the store verifies lifecycle safety."""
        await self.repository.remove_allocation(
            allocation_id=allocation_id, expected_revision=expected_revision
        )

    def allocation_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """Use the same public projection for command responses and queries."""
        return self.queries.allocation_view(row)

    async def operations(self, *, request_id: str | None = None) -> dict[str, Any]:
        """Recover submitted work even after the browser lost its response."""
        rows = await self.repository.list_operations(request_id=request_id)
        return {"operations": [await self.operation_view(row) for row in rows]}

    async def retirement_impact(self, allocation_id: str) -> dict[str, Any]:
        """Read the full release scope from one database snapshot."""
        return await self.queries.retirement_impact(allocation_id)

    async def operation_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """Check the irreversible boundary before advertising cancellation."""
        allocation = (
            await self.repository.get_allocation(row["allocation_id"])
            if row.get("allocation_id")
            else None
        )
        result = public_operation(
            row, release_committed=bool(allocation and allocation.get("release_committed_at"))
        )
        if (
            row["kind"] == "acquire"
            and row["state"] == "unknown"
            and allocation
            and not allocation.get("external_id")
            and not row.get("result", {}).get("resource_ref")
        ):
            result["allowed_actions"].append("resolve")
        if autoscaling_operation_is_terminal(row, allocation):
            result["allowed_actions"] = [
                action for action in result["allowed_actions"] if action not in {"retry", "cancel"}
            ]
        elif autoscaling_retirement_needs_settlement(row, allocation):
            result["allowed_actions"] = [
                action for action in result["allowed_actions"] if action != "cancel"
            ]
        if autoscaling_acquire_can_cancel(row, allocation):
            result["allowed_actions"].append("cancel")
        if (
            allocation
            and allocation.get("release_committed_at")
            and row["kind"] not in {"retire", "inspect", "discard"}
        ):
            # The dispatcher will not claim ordinary mutations after release.
            # Do not advertise an action that can only leave the operation queued.
            result["allowed_actions"] = []
        return result

    async def resolve_operation(self, operation_id: str, external_id: str) -> dict[str, Any]:
        """Record a deliberate adoption; the worker verifies its provider scope before deployment."""
        operation = await self.repository.get_operation(operation_id)
        if operation is None:
            raise ComputeRefusal("operation_not_found", "The operation does not exist.", status=404)
        if operation["kind"] != "acquire" or operation["state"] != "unknown":
            raise ComputeRefusal(
                "resolution_not_allowed", "Only an unknown acquisition can be resolved this way."
            )
        if operation.get("result", {}).get("resource_ref"):
            raise ComputeRefusal(
                "resolution_not_allowed",
                "This operation already identified its resource; retry observation.",
            )
        allocation = await self._allocation(operation["allocation_id"])
        if allocation.get("release_committed_at"):
            raise ComputeRefusal(
                "resolution_not_allowed", "This allocation has already been released."
            )
        entry = self.registry.get(allocation["provider_id"])
        config = entry.provider.validate_config(
            "attach", {**allocation["configuration"], "external_id": external_id}
        )
        resolved = await self.repository.resolve_acquisition(
            operation_id=operation_id, external_id=config["external_id"]
        )
        return await self.operation_view(resolved)

    async def queue_inspection(self, row: dict[str, Any], *, request_key: str) -> None:
        """Schedule an observation without mutating the external resource."""
        parameters = {"allocation_id": row["id"]}
        await self.repository.create_operation(
            request_id=request_key,
            fingerprint=fingerprint("inspect", parameters),
            kind="inspect",
            allocation_id=row["id"],
            parameters=parameters,
            requested_by=None,
        )

    async def _existing(
        self, request_id: str, kind: str, parameters: dict[str, Any]
    ) -> dict[str, Any] | None:
        rows = await self.repository.list_operations(request_id=request_id)
        if not rows:
            return None
        row = rows[0]
        if row["fingerprint"] != fingerprint(kind, parameters):
            raise ComputeRefusal(
                "idempotency_conflict", "This request ID belongs to different parameters."
            )
        return await self.operation_view(row)

    async def _connection(
        self, connection_id: str, provider_id: str | None = None
    ) -> dict[str, Any]:
        row = await self.repository.get_connection(connection_id)
        if row is None:
            raise ComputeRefusal(
                "connection_not_found", "The selected connection no longer exists.", status=404
            )
        if provider_id is not None and row["provider_id"] != provider_id:
            raise ComputeRefusal(
                "connection_provider_mismatch",
                "The connection belongs to a different provider.",
                status=422,
            )
        return row

    async def _allocation(self, allocation_id: str) -> dict[str, Any]:
        row = await self.repository.get_allocation(allocation_id)
        if row is None:
            raise ComputeRefusal(
                "allocation_not_found", "The allocation does not exist.", status=404
            )
        return row

    async def _deployment_profile(self, requested: str | None) -> DeploymentProfile:
        """Choose configured setup capacity without sharing a live sandbox manager endpoint."""
        profiles = await self.deployment_profiles()
        reserved_reader = getattr(self.repository, "list_reserved_deployment_profiles", None)
        reserved = set(await reserved_reader()) if reserved_reader is not None else set()
        if requested is not None:
            profile = profiles.get(requested)
            if profile is not None:
                if requested in reserved:
                    raise ComputeRefusal(
                        "deployment_profile_reserved",
                        "The requested server setup is reserved for CloudLab autoscaling.",
                    )
                return profile
            raise ComputeRefusal(
                "profile_unavailable", "The requested server setup is not configured.", status=422
            )
        if not profiles:
            raise ComputeRefusal(
                "profile_unavailable",
                "Server setup is not configured on the control plane.",
                status=422,
            )
        allocations = await self.repository.list_allocations()
        occupied = {
            (row.get("deployment_profile") or {}).get("id")
            for row in allocations
            if row.get("observed_state") not in {"terminated", "absent"}
        }
        for profile in profiles.values():
            if profile.id not in occupied and profile.id not in reserved:
                # The repository rechecks occupancy under its allocation lock.
                # Concurrent requests may safely refuse rather than share a route.
                return profile
        raise ComputeRefusal(
            "profile_unavailable",
            "All configured server setups are in use. Configure another setup before adding a server.",
            status=409,
        )

    async def _configuration(
        self,
        provider_id: str,
        mode: str,
        connection_id: str,
        ssh_connection_id: str | None,
        supplied: dict[str, Any],
        *,
        deploy: bool,
    ) -> tuple[ComputeProviderRegistration, dict[str, Any]]:
        entry = self.registry.get(provider_id)
        await self._connection(connection_id, provider_id)
        if not self.secrets.configured:
            raise ComputeRefusal(
                "secret_store_unavailable", "Compute secret storage is not configured.", status=503
            )
        if entry.ssh_credential_fields is not None and deploy and not ssh_connection_id:
            raise ComputeRefusal(
                "ssh_connection_required", "Select a deployment SSH connection.", status=422
            )
        if ssh_connection_id:
            await self._connection(ssh_connection_id, "existing_ssh")
        effective = dict(supplied)
        if provider_id == "cloudlab":
            defaults = await self.cloudlab_defaults()
            for field in entry.fields:
                key = field["key"]
                if field.get("modes") and mode not in field["modes"]:
                    continue
                if field.get("deployment_only") and not deploy:
                    continue
                value = defaults.get(key)
                if key not in effective and value is not None and value != "":
                    effective[key] = value
        config = entry.configuration(effective, connection_id, ssh_connection_id)
        return entry, entry.provider.validate_config(mode, config)

    def _require_managed(self, row: dict[str, Any], capability: str) -> None:
        caps = self.registry.get(row["provider_id"]).provider.capabilities()
        supported = caps.can_extend if capability == "extend" else caps.can_release
        if not supported:
            raise ComputeRefusal(
                "unsupported_capability", "The provider does not support this resource operation."
            )
        if row.get("management_mode") != "manage":
            raise ComputeRefusal(
                "unmanaged_scope",
                "Explicitly adopt resource management before changing its lifecycle.",
            )
        if row.get("release_committed_at") or row.get("observed_state") == "terminated":
            raise ComputeRefusal(
                "release_committed", "The resource is already releasing or terminated."
            )
        if capability == "release" and not row.get("deployment_profile"):
            raise ComputeRefusal(
                "inventory_unavailable",
                "Attach a deployment profile and configured SSH connection before releasing this resource.",
            )

    @staticmethod
    def _require_manual_policy_control(row: dict[str, Any]) -> None:
        """Keep per-node renewal and replacement inside the owning pool policy."""
        if row.get("autoscaling_owned"):
            raise ComputeRefusal(
                "autoscaling_managed",
                "This server's renewal policy and node replacement are managed by CloudLab autoscaling.",
            )
