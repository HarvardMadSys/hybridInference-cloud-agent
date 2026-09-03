"""Replica-local shared-sandbox claim compatibility."""

from cloud_agent.execution_profiles import (
    compatible_shared_sandbox_followup_attested,
    shared_sandbox_claim_attested,
)
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
    SHARED_SANDBOX_V1_HELPER_PINS,
    shared_deployment_capability,
)


def _attestation(*, protocol: str, helpers: dict[str, str]) -> dict[str, object]:
    return {
        "sandbox_image_digest": "sha256:image",
        "shared_sandbox_protocol": protocol,
        "runner_capabilities": [
            SHARED_SANDBOX_CAPABILITY,
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability("target-sha"),
        ],
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": helpers,
    }


def test_v1_owner_needs_only_the_reviewed_followup_helper_floor() -> None:
    legacy = _attestation(protocol="v1", helpers=dict(SHARED_SANDBOX_V1_HELPER_PINS))

    assert compatible_shared_sandbox_followup_attested(legacy)
    assert not shared_sandbox_claim_attested(legacy, expected_deployment_sha="target-sha")


def test_current_protocol_still_requires_every_current_helper() -> None:
    current = _attestation(
        protocol=SHARED_SANDBOX_PROTOCOL,
        helpers=dict(SHARED_SANDBOX_HELPER_PINS),
    )
    missing_observer = _attestation(
        protocol=SHARED_SANDBOX_PROTOCOL,
        helpers=dict(SHARED_SANDBOX_V1_HELPER_PINS),
    )

    assert compatible_shared_sandbox_followup_attested(current)
    assert shared_sandbox_claim_attested(current, expected_deployment_sha="target-sha")
    assert not compatible_shared_sandbox_followup_attested(missing_observer)


def test_v1_owner_without_the_sdk_pin_is_rejected() -> None:
    legacy = _attestation(protocol="v1", helpers={})

    assert not compatible_shared_sandbox_followup_attested(legacy)
