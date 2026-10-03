"""Server-owned execution profiles for sandbox scheduling.

An execution profile is a platform policy, not job metadata.  Callers choose
what work to run; the deployment chooses the minimum boundary that work must
run behind.  Keeping the value in its own column makes that distinction
enforceable at claim time and inheritable across a thread.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum

from cloud_agent_shared.shared_protocol import (
    FORK_HELPER,
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    has_shared_deployment_capability,
)


class ExecutionProfile(StrEnum):
    """The execution boundaries the scheduler currently understands."""

    TRUSTED_V1 = "trusted-v1"
    UNTRUSTED_KATA_V1 = "untrusted-kata-v1"


ENV_DEFAULT_EXECUTION_PROFILE = "AGENT_DEFAULT_EXECUTION_PROFILE"
KATA_RUNTIME = "io.containerd.kata.v2"
RESERVED_EXECUTION_METADATA_KEYS = frozenset(
    {
        "requires",
        "execution_profile",
        "_agent_execution_profile",
        "sandbox_runtime",
        "sandbox_image_digest",
        "runtime_versions",
        "helper_versions",
        "runner_capabilities",
        "host_kernel",
        "guest_kernel",
    }
)


def _shared_sandbox_claim_attested(
    attestation: Mapping[str, object], *, creates_group: bool
) -> bool:
    """Validate the image/runtime proof both claim gates share.

    Only a replica that may create a group must prove the fork helper. A Fork
    of that group runs it inside the group's own container; an ordinary turn
    never runs it.
    """
    digest = attestation.get("sandbox_image_digest")
    if not isinstance(digest, str) or not digest.strip():
        return False
    capabilities = attestation.get("runner_capabilities")
    if not isinstance(capabilities, list) or SHARED_SANDBOX_CAPABILITY not in capabilities:
        return False
    versions = attestation.get("runtime_versions")
    if not isinstance(versions, Mapping) or any(
        versions.get(runtime_id) != version
        for runtime_id, version in SHARED_SANDBOX_RUNTIME_PINS.items()
    ):
        return False
    helpers = attestation.get("helper_versions")
    return isinstance(helpers, Mapping) and all(
        helpers.get(helper_id) == version
        for helper_id, version in SHARED_SANDBOX_HELPER_PINS.items()
        if creates_group or helper_id != FORK_HELPER
    )


def shared_sandbox_claim_attested(
    attestation: Mapping[str, object],
    *,
    expected_deployment_sha: str | None = None,
) -> bool:
    """Whether this exact replica may create a group or claim new work.

    A host heartbeat is machine-level and is shared by every runner replica on
    that machine. During a rolling deployment it can therefore describe a new
    replica while an old replica is the one actually polling. The claim carries
    this second, replica-local proof so an old or partially configured process
    skips shared work instead of borrowing its sibling's advertised capability.
    Creating a group additionally requires the completed deployment marker;
    readiness and scheduling therefore enforce the same exact-SHA fence.
    """
    capabilities = attestation.get("runner_capabilities")
    return (
        _shared_sandbox_claim_attested(attestation, creates_group=True)
        and isinstance(capabilities, list)
        and NATIVE_FORK_AT_ANCHOR_CAPABILITY in capabilities
        and has_shared_deployment_capability(
            capabilities,
            expected_deployment_sha=expected_deployment_sha,
        )
    )


def compatible_shared_sandbox_followup_attested(attestation: Mapping[str, object]) -> bool:
    """Whether a replica may continue a retained group it already owns.

    A follow-up needs neither the completed target deployment nor the fork
    helper. During a rolling deployment a host that owns groups may stay on the
    previous release for ordinary follow-ups, and a release from before the
    fork helper had a label reports no version for it; an ordinary turn never
    runs it. Every Agent CLI and every other helper must still match exactly,
    so a replica whose turn-time contract changed stays refused until its
    compatibility is reviewed here.
    """
    return _shared_sandbox_claim_attested(attestation, creates_group=False)


def default_execution_profile(env: Mapping[str, str] | None = None) -> ExecutionProfile:
    """Return the deployment policy for newly created root threads.

    Existing deployments default to ``trusted-v1`` so upgrading the control
    plane cannot strand their queue.  Opting into public/untrusted work is an
    explicit deployment change, and an invalid value refuses job creation
    instead of silently weakening to the trusted profile.
    """
    source = env if env is not None else os.environ
    raw = (source.get(ENV_DEFAULT_EXECUTION_PROFILE) or ExecutionProfile.TRUSTED_V1).strip()
    try:
        return ExecutionProfile(raw)
    except ValueError as exc:
        allowed = ", ".join(profile.value for profile in ExecutionProfile)
        raise ValueError(
            f"{ENV_DEFAULT_EXECUTION_PROFILE} must be one of: {allowed}; got {raw!r}"
        ) from exc
