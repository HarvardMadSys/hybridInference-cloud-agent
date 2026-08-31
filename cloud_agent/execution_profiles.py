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

from cloud_agent_host.shared_protocol import (
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
)


class ExecutionProfile(StrEnum):
    """The execution boundaries the scheduler currently understands."""

    TRUSTED_V1 = "trusted-v1"
    UNTRUSTED_KATA_V1 = "untrusted-kata-v1"


ENV_DEFAULT_EXECUTION_PROFILE = "AGENT_DEFAULT_EXECUTION_PROFILE"
ENV_SHARED_LIVE_SANDBOX = "AGENT_SHARED_LIVE_SANDBOX"
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
        "shared_sandbox_protocol",
        "runner_capabilities",
        "host_kernel",
        "guest_kernel",
    }
)


def shared_sandbox_claim_attested(attestation: Mapping[str, object]) -> bool:
    """Whether this exact polling runner replica may claim shared-live work.

    A host heartbeat is machine-level and is shared by every runner replica on
    that machine. During a rolling deployment it can therefore describe a new
    replica while an old replica is the one actually polling. The claim carries
    this second, replica-local proof so an old or partially configured process
    skips shared work instead of borrowing its sibling's advertised capability.
    """
    digest = attestation.get("sandbox_image_digest")
    if not isinstance(digest, str) or not digest.strip():
        return False
    if attestation.get("shared_sandbox_protocol") != SHARED_SANDBOX_PROTOCOL:
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
    )


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
