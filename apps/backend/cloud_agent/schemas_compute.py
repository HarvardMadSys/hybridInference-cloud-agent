"""Write contracts for administrator-owned compute resources.

Provider configuration is checked by the selected adapter before persistence.
Secrets have their own write-only field and never enter operation requests.
"""

from __future__ import annotations

import base64
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from cloud_agent.ports.external.host_bootstrap import validate_sandbox_storage_path


class ComputeRequest(BaseModel):
    """Reject misspelled commands rather than silently dropping controls."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CloudLabDefaults(ComputeRequest):
    """Non-secret defaults applied to new CloudLab server requests."""

    project: str = Field(default="", max_length=128)
    duration: int = Field(default=4, ge=1, le=168)
    profile_project: str = Field(default="", max_length=128)
    profile_name: str = Field(default="", max_length=128)
    ssh_username: str = Field(
        default="", max_length=128, pattern=r"^(?:[a-zA-Z_][a-zA-Z0-9_.-]*)?$"
    )


class RenewalPolicy(ComputeRequest):
    """Independent resource deadlines, expressed in seconds.

    With rotation, ``renew_before_seconds`` is also when a node is replaced:
    an extension is tried first; a refused one, or a node at its maximum age,
    starts a move of the whole Host to a newly allocated node instead.
    """

    enabled: bool = False
    renew_before_seconds: int = Field(default=3600, ge=60, le=604800)
    extend_to_horizon_seconds: int = Field(default=14400, ge=120, le=2592000)
    stop_new_groups_before_seconds: int = Field(default=1800, ge=0, le=604800)
    stop_claims_before_seconds: int = Field(default=300, ge=0, le=604800)
    reason: str = Field(default="", max_length=2000)
    rotation_enabled: bool = False
    max_node_age_seconds: int = Field(default=604800, ge=3600, le=2592000)
    # Hardware for a replacement node, tried in order (newest first).
    node_types: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")]] = Field(
        default_factory=list, max_length=20
    )
    # The profile parameter that selects a node's hardware type.
    node_type_binding: str = Field(default="phystype", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

    @model_validator(mode="after")
    def ordered_deadlines(self) -> Self:
        """Renew early enough to react before either admission deadline."""
        if not (
            self.extend_to_horizon_seconds
            > self.renew_before_seconds
            > self.stop_new_groups_before_seconds
            >= self.stop_claims_before_seconds
        ):
            raise ValueError("require horizon > renew_before > stop_new_groups >= stop_claims")
        if self.enabled and not self.reason:
            raise ValueError("an actual reason is required to enable automatic renewal")
        if self.rotation_enabled and (not self.enabled or not self.node_types):
            raise ValueError("node replacement needs automatic renewal and candidate node types")
        if len(set(self.node_types)) != len(self.node_types):
            raise ValueError("candidate node types must be distinct")
        return self


class AddComputeConnection(ComputeRequest):
    """Create a named, write-only credential in the control plane."""

    name: str = Field(min_length=1, max_length=120)
    provider_id: Literal["existing_ssh", "cloudlab"]
    configuration: dict[str, Any] = Field(default_factory=dict)
    secret: SecretStr = Field(min_length=1, max_length=65536)


class UpdateComputeConnection(ComputeRequest):
    """Replace a credential only against the revision the editor observed."""

    expected_revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    configuration: dict[str, Any] | None = None
    secret: SecretStr | None = Field(default=None, min_length=1, max_length=65536)


class AddDeploymentProfile(ComputeRequest):
    """Create a reviewed Host deployment profile from write-only Admin inputs."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    deployment_sha: str = Field(pattern=r"^[a-f0-9]{40}$")
    deploy_root: str = Field(default="/opt/cloud-agent", pattern=r"^/[a-zA-Z0-9/_-]+$")
    environment: SecretStr = Field(min_length=1, max_length=65536)
    files: dict[str, Annotated[SecretStr, Field(min_length=1, max_length=65536)]] = Field(
        min_length=1, max_length=24
    )
    source_bundle_base64: SecretStr = Field(min_length=1)
    install_prerequisites: bool = True
    timeout_seconds: int = Field(default=3600, ge=60, le=7200)
    data_mount: str | None = Field(default=None, pattern=r"^/[a-zA-Z0-9/_-]+$")

    def bundle(self) -> bytes:
        """Decode with a hard ceiling before the value reaches persistence."""
        try:
            encoded = self.source_bundle_base64.get_secret_value().encode("ascii")
            if len(encoded) > 96 * 1024 * 1024:
                raise ValueError
            payload = base64.b64decode(encoded, validate=True)
        except (UnicodeError, ValueError):
            raise ValueError("invalid deployment bundle") from None
        if not payload or len(payload) > 64 * 1024 * 1024:
            raise ValueError("invalid deployment bundle")
        return payload


class AddServerRequest(ComputeRequest):
    """Acquire or attach a resource and deploy a configured Host profile."""

    request_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    provider_id: Literal["existing_ssh", "cloudlab"]
    mode: Literal["attach", "provision"]
    connection_id: str = Field(min_length=1, max_length=128)
    ssh_connection_id: str | None = Field(default=None, min_length=1, max_length=128)
    configuration: dict[str, Any] = Field(default_factory=dict)
    deployment_profile: str | None = Field(default=None, min_length=1, max_length=120)
    sandbox_storage_path: str | None = Field(default=None, max_length=4096)
    policy: RenewalPolicy = Field(default_factory=RenewalPolicy)

    @field_validator("sandbox_storage_path")
    @classmethod
    def validate_storage_path(cls, value: str | None) -> str | None:
        """An omitted value preserves the selected deployment profile's storage."""
        return None if value is None else validate_sandbox_storage_path(value)


class ImportComputeRequest(ComputeRequest):
    """Observe an explicitly named existing cloud allocation without deploying."""

    request_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    provider_id: Literal["cloudlab"]
    connection_id: str = Field(min_length=1, max_length=128)
    configuration: dict[str, Any] = Field(default_factory=dict)


class UpdateComputePolicy(ComputeRequest):
    """Compare and replace the renewal policy and explicit management scope."""

    expected_revision: int = Field(ge=0)
    policy: RenewalPolicy
    management_mode: Literal["observe", "manage"] | None = None


class ExtendComputeRequest(ComputeRequest):
    """A fixed absolute deadline survives retries without adding more hours."""

    request_id: str = Field(min_length=1, max_length=128)
    target_expires_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=2000)


class RotateComputeRequest(ComputeRequest):
    """Move the Host to a replacement node now, before its lease forces it."""

    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)


class RetireComputeRequest(ComputeRequest):
    """Request retirement against the allocation scope the admin reviewed."""

    request_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)


class ComputeOperationAction(ComputeRequest):
    """An explicit action whose current state is checked under the operation lock."""


class ResolveComputeOperation(ComputeRequest):
    """Adopt an explicitly identified result after an acquisition response was lost."""

    external_id: str = Field(min_length=1, max_length=512)
