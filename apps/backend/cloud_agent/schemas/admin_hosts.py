"""Administrative host-pool views, placement, drain fences, and runtime settings."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt, field_validator

from cloud_agent.capacity_protocol import (
    AgentHostCapacity,  # noqa: TC001 — Pydantic resolves annotations at runtime.
)
from cloud_agent.schemas._sandbox_manager_wire import _SandboxManagerWireNames
from cloud_agent_shared.host_settings import (
    HOST_SETTINGS,
    MAX_SETTINGS_KEYS,
    SettingScope,
    SettingUnit,
    validate_admin_change,
)
from cloud_agent_shared.shared_protocol import (
    HOST_CONCURRENCY_MAX_SLOTS,
    HOST_CONCURRENCY_MIN_SLOTS,
)


class AgentRunnerHost(_SandboxManagerWireNames):
    """One machine in the agent runner pool.

    Most fields below ``seconds_since_seen`` arrived with the dynamic pool
    (phase G) and retain defaults for hosts that joined by polling. The drain
    fence epoch is different: its migration backfills every host and the API
    requires it so an automation client never mistakes a partial fence
    contract for a safe compare-and-swap base.
    """

    host: str
    active: bool
    last_worker_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    seconds_since_seen: float = Field(
        ...,
        description=(
            "Age of the last claim poll, by the server's clock. A runner polls "
            "only between jobs, so a busy machine can look quiet for as long as "
            "its longest job."
        ),
    )
    status: str = Field(
        "online",
        description=(
            "Pool lifecycle state: enrolling, online, draining, disabled or "
            "offline. Only an online host takes new claims."
        ),
    )
    enrolled: bool = Field(
        False,
        description=(
            "Whether this host holds a credential issued by enrollment. False "
            "means it joined by polling and is not expected to heartbeat, so "
            "the offline reaper leaves it alone."
        ),
    )
    labels: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str] = Field(
        default_factory=list,
        description="What this machine can run. A job may only go to a host declaring all of it.",
    )
    slots_total: int | None = Field(
        None,
        description=("The concurrency limit this host says it is serving. Null means unmetered."),
    )
    slots_used: int = Field(0, description="Attempts currently holding one of this host's slots.")
    slots_desired: int | None = Field(
        None,
        description=(
            "Turns at once an operator asked this host to run, set from the "
            "admin page; the host applies it on its next heartbeat and reports "
            "it back as slots_total. The claim admits against the lower of the "
            "two. Null means the host runs its own default."
        ),
    )
    launch_error: str | None = Field(
        None,
        description=(
            "Why this host is claiming nothing despite having room — it could "
            "not start the process a turn runs in, repeatedly. Empty means "
            "nothing is wrong; null means this host does not report it."
        ),
    )
    runner_version: str | None = None
    sandbox_manager_url: str | None = Field(
        None,
        description="Where this host's sandbox manager listens, as the control plane reaches it.",
    )
    broker_url: str | None = Field(
        default=None, deprecated=True, description="Compatibility alias for sandbox_manager_url."
    )
    last_heartbeat_at: datetime | None = None
    drain_fence_token: UUID | None = Field(
        None,
        repr=False,
        description=(
            "Opaque owner of an automated deployment drain, retained after release "
            "as an idempotency receipt."
        ),
    )
    drain_fence_status_before: Literal["online", "draining"] | None = Field(
        None,
        description="Host status immediately before the current drain-fence token was acquired.",
    )
    drain_fence_active: bool = Field(
        False,
        description=(
            "True only while the recorded deployment fence owns a draining host; "
            "false for an absent fence or released receipt."
        ),
    )
    drain_fence_epoch: UUID = Field(
        ...,
        description=(
            "Opaque compare-and-swap generation for deployment drain state. It rotates "
            "whenever host lifecycle, credential, placement, or drain ownership changes."
        ),
    )
    removal_pending: bool = Field(
        False,
        description=(
            "True while an operator's Remove waits for this draining host's running "
            "attempts to end; the host is revoked once they have."
        ),
    )
    sandbox_runtime: str | None = Field(
        None,
        description=(
            "Attested container runtime for this host's jobs. '' = "
            "shared-kernel runc; null = the host has never reported one."
        ),
    )
    sandbox_image_digest: str | None = Field(
        None,
        description="Digest of the sandbox image this host launches, as last reported.",
    )
    runtime_versions: dict[str, str] | None = Field(
        None,
        description=(
            "The *default* CLI version per runtime id — what a job runs when "
            "nothing has been selected."
        ),
    )
    runtime_versions_available: dict[str, list[str]] | None = Field(
        None,
        description=(
            "Every CLI version this host's image holds, keyed by runtime id, "
            "default first. The menu an admin may select from; the control "
            "plane cannot serve a version absent from it."
        ),
    )
    capacity_epoch: str | None = Field(
        None,
        description=(
            "Generation of this host's capacity data; rotated by re-enrollment. "
            "A client uses it to drop a curve that belongs to the machine this "
            "one replaced."
        ),
    )
    capacity: AgentHostCapacity | None = Field(
        None,
        description=(
            "The last accepted host observation, with the server's own "
            "received/sampled times and freshness. Null means no usable "
            "observation has ever arrived — which is not the same as an idle "
            "machine, and is rendered as unavailable rather than as zero."
        ),
    )
    settings_desired: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Runtime settings set for this server on the admin page, by key "
            "(see host_setting_definitions). They win over the global values; "
            "a setting absent here uses the global value, or the deployment default."
        ),
    )
    settings_applied: dict[str, int | float] | None = Field(
        None,
        description=(
            "The runtime settings this host reported applying on its last "
            "heartbeat. Null means it has never reported — a runner that "
            "predates admin settings, or one that has not beaten since it "
            "enrolled."
        ),
    )
    settings_local: dict[str, int | float] | None = Field(
        None,
        description=(
            "The host's original bootstrap environment values, for diagnostics "
            "and one-time migration. Clearing an admin value restores the global "
            "value or built-in default."
        ),
    )
    sandbox_archive_settings_applied_revision: StrictInt | None = Field(
        None,
        ge=0,
        description=(
            "Archive-policy revision this host's sandbox manager confirmed applying. Null means "
            "the host predates database-backed archive settings or has not synchronized yet."
        ),
    )
    untrusted_without_disk_quota: bool = Field(
        False,
        description=(
            "True when this host can be given untrusted (Kata) work and reports "
            "applying no container disk quota (sandbox_disk_mib 0), so a new "
            "untrusted sandbox's writable layer is unbounded on a disk every other "
            "sandbox on the host shares. A recommendation only: nothing is refused "
            "or rescheduled. False when the host has not reported the setting."
        ),
    )


class AcquireHostDrainFenceRequest(BaseModel):
    """Atomically replace the expected drain owner with a new opaque owner."""

    expected_fence_token: UUID | None = Field(..., repr=False)
    expected_fence_epoch: UUID = Field(
        ...,
        repr=False,
        description=(
            "Canonical UUID copied from the latest host snapshot; the acquire is stale if "
            "drain-relevant host state changed afterward."
        ),
    )
    fence_token: UUID = Field(..., repr=False)

    @field_validator("expected_fence_epoch", mode="before")
    @classmethod
    def expected_epoch_is_canonical(cls, value: object) -> object:
        """Reject alternate UUID spellings so persisted CAS intent is byte-stable."""
        if isinstance(value, UUID) or not isinstance(value, str):
            return value
        try:
            parsed = UUID(value)
        except ValueError:
            return value  # Let Pydantic produce its ordinary UUID error.
        if str(parsed) != value:
            raise ValueError("expected_fence_epoch must be a canonical UUID")
        return parsed


class ReleaseHostDrainFenceRequest(BaseModel):
    """Release only the automated drain owned by this opaque token."""

    fence_token: UUID = Field(..., repr=False)


class HostSettingDefinition(BaseModel):
    """One runtime setting an admin may set, with its unit and bounds."""

    key: str
    env: str = Field(
        ..., description="The legacy host environment variable imported at enrollment."
    )
    label: str
    description: str
    unit: SettingUnit
    default: int = Field(..., description="Built-in value when no admin override is set.")
    minimum: int
    maximum: int
    scope: SettingScope = Field(
        ...,
        description=(
            "Which host process applies it: the runner, as it starts each turn; "
            "the sandbox manager, which runs terminals and the idle sweep; or both."
        ),
    )


def host_setting_definitions() -> list[HostSettingDefinition]:
    """Every runtime setting an admin may set, in the order the page lists them."""
    return [
        HostSettingDefinition(
            key=setting.key,
            env=setting.env,
            label=setting.label,
            description=setting.description,
            unit=setting.unit,
            default=setting.default,
            minimum=setting.minimum,
            maximum=setting.maximum,
            scope=setting.scope,
        )
        for setting in HOST_SETTINGS
    ]


class ListAgentRunnerHostsResponse(BaseModel):
    """Runner pool, new-chat placement, and selected CLI versions."""

    execution_profile: str = Field(
        "untrusted-kata-v1",
        description="Server-owned policy for new chats; used to show eligible servers in admin.",
    )
    hosts: list[AgentRunnerHost]
    active_host: str | None = Field(
        None,
        description=(
            "Host that may materialize chats without a sandbox, or null when "
            "any claimable host may. Retained groups stay on their owning host."
        ),
    )
    runtime_versions: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "The deployment's chosen CLI version per runtime id. A runtime "
            "absent here runs its claiming host's default."
        ),
    )
    host_settings: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Runtime settings set for every server. A value set for one server "
            "wins over these; a setting set nowhere uses the deployment default."
        ),
    )
    host_setting_definitions: list[HostSettingDefinition] = Field(
        default_factory=list,
        description="Every runtime setting an admin may set, with its unit and bounds.",
    )
    observed_at: datetime | None = Field(
        None,
        description=(
            "When the database answered this read, by its own clock. Every "
            "pool response carries it, so an operation's answer and a poll's "
            "answer are comparable without the client's clock."
        ),
    )


class SetActiveAgentRunnerHostRequest(BaseModel):
    """Pin new-chat placement to one host, or unpin with ``null``."""

    host: str | None = Field(
        None,
        max_length=253,
        description="A host that has polled at least once, or null to unpin.",
    )


class SetRuntimeVersionRequest(BaseModel):
    """Choose the CLI version this deployment runs for one runtime."""

    runtime: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Runtime id, as the registry knows it (e.g. 'claude-code').",
    )
    version: str | None = Field(
        None,
        max_length=128,
        description=(
            "A version some host in the pool offers, or null to return this "
            "runtime to the image default. Applies to new threads; a thread "
            "already running keeps the version it started on."
        ),
    )


class SetHostSlotsRequest(BaseModel):
    """Ask one host to run this many turns at once, or with ``null`` its default."""

    slots: int | None = Field(
        None,
        ge=HOST_CONCURRENCY_MIN_SLOTS,
        le=HOST_CONCURRENCY_MAX_SLOTS,
        description=(
            "Turns this host should run at once. At least two, because the "
            "shared-live protocol needs a fork's sibling to run beside its "
            "parent rather than behind it; at most 128, a typo guard the host "
            "checks again. Neither bound says what a machine can carry. The "
            "host applies the number on its next heartbeat, starts a process "
            "per turn as it claims one, and never interrupts a turn already "
            "running. Null returns the host to its AGENT_HOST_SLOTS default."
        ),
    )


class SetHostSettingsRequest(BaseModel):
    """Set or clear runtime settings, for every server or for one."""

    settings: dict[str, StrictInt | None] = Field(
        ...,
        min_length=1,
        max_length=MAX_SETTINGS_KEYS,
        description=(
            "The settings to change, by key (see host_setting_definitions), each "
            "a whole number inside that setting's bounds, or null to clear it so "
            "the next level applies: a server's value falls back to the global "
            "one, and a global value to the deployment's built-in default. "
            "Settings not named keep their values. Hosts apply the result from "
            "their next heartbeat answer."
        ),
    )

    @field_validator("settings")
    @classmethod
    def _known_and_in_bounds(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        return validate_admin_change(value)
