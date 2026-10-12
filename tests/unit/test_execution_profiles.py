"""Replica-local shared-sandbox claim compatibility."""

import pytest

from cloud_agent.execution_profiles import (
    compatible_shared_sandbox_followup_attested,
    shared_sandbox_claim_attested,
)
from cloud_agent.schemas import WorkerClaimAttestation
from cloud_agent_host.shared_protocol import (
    NATIVE_FORK_AT_ANCHOR_CAPABILITY,
    SHARED_SANDBOX_CAPABILITY,
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_RUNTIME_PINS,
    shared_deployment_capability,
)
from cloud_agent_shared.shared_protocol import FORK_HELPER

TARGET = "target-sha"

#: The claim a runner of 3750f43a sends, the release from before the fork helper
#: had its own label: produced by that commit's ``_claim_attestation`` from its
#: sandbox image's labels, with only the digest and deployment made up. It still
#: names the shared-sandbox protocol, which this control plane no longer reads.
#: Delete it together with the runner's translation for older control planes.
PREVIOUS_RELEASE_CLAIM = {
    "guest_kernel": None,
    "helper_versions": {
        "claude-agent-sdk": "0.2.148",
        "guest-observer": "2",
        "http-agent-live-bridge": "2",
        "member-window": "1",
        "tool-mark": "1",
    },
    "host_kernel": "24.6.0",
    "runner_capabilities": [
        "shared-sandbox-v1",
        "native-fork-at-anchor-v1",
        "goal_report_v1",
        "mid-turn-input-v1:claude-code",
        "mid-turn-input-v1:codex",
        "shared-sandbox-deployment:previous-sha",
    ],
    "runtime_versions": {
        "claude-code": "2.1.220",
        "codex": "0.145.0",
        "kilo": "7.4.17",
        "opencode": "1.18.9",
        "pi": "0.73.1",
    },
    "runtime_versions_available": {
        "claude-code": ["2.1.220"],
        "codex": ["0.145.0"],
        "kilo": ["7.4.17"],
        "opencode": ["1.18.9"],
        "pi": ["0.73.1"],
    },
    "sandbox_image_digest": "sandbox@sha256:previous",
    "sandbox_runtime": "",
    "shared_sandbox_protocol": "v2",
}


def _as_the_api_reads_it(claim: dict[str, object]) -> dict[str, object]:
    """What the claim route hands to ``claim_job``."""
    return WorkerClaimAttestation.model_validate(claim).model_dump()


def _current(**overrides: object) -> dict[str, object]:
    claim: dict[str, object] = {
        "sandbox_image_digest": "sha256:image",
        "runner_capabilities": [
            SHARED_SANDBOX_CAPABILITY,
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability(TARGET),
        ],
        "runtime_versions": dict(SHARED_SANDBOX_RUNTIME_PINS),
        "helper_versions": dict(SHARED_SANDBOX_HELPER_PINS),
    }
    claim.update(overrides)
    return claim


def _helpers_without(helper_id: str) -> dict[str, str]:
    return {key: value for key, value in SHARED_SANDBOX_HELPER_PINS.items() if key != helper_id}


def test_the_current_image_may_continue_its_groups_and_create_new_ones() -> None:
    current = _as_the_api_reads_it(_current())

    assert compatible_shared_sandbox_followup_attested(current)
    assert shared_sandbox_claim_attested(current, expected_deployment_sha=TARGET)


def test_the_previous_http_bridge_cannot_take_native_question_replies() -> None:
    """Keep the historical claim intact: helper 2 cannot handle question replies.

    Missing the fork helper alone still permits follow-ups, as tested below.
    A changed turn-time helper does not: the new host sends a question command
    that this previous image cannot execute.
    """
    previous = _as_the_api_reads_it(PREVIOUS_RELEASE_CLAIM)

    assert "shared_sandbox_protocol" not in previous
    assert not compatible_shared_sandbox_followup_attested(previous)
    assert not shared_sandbox_claim_attested(previous, expected_deployment_sha=TARGET)
    assert not shared_sandbox_claim_attested(previous, expected_deployment_sha="previous-sha")


@pytest.mark.parametrize("helper_id", sorted(set(SHARED_SANDBOX_HELPER_PINS) - {FORK_HELPER}))
def test_every_other_helper_is_required_for_a_follow_up_too(helper_id: str) -> None:
    missing = _current(helper_versions=_helpers_without(helper_id))

    assert not compatible_shared_sandbox_followup_attested(missing)
    assert not shared_sandbox_claim_attested(missing, expected_deployment_sha=TARGET)


@pytest.mark.parametrize("fork_helper", [None, "1"])
def test_only_creating_a_group_needs_the_pinned_fork_helper(fork_helper: str | None) -> None:
    """A Fork of a new group runs that group's own helper; a follow-up runs none.

    A helper that ignores the anchor would fork the latest message instead of
    the one the owner chose, so creating a group needs it at the exact pin.
    """
    helpers = _helpers_without(FORK_HELPER)
    if fork_helper is not None:
        helpers[FORK_HELPER] = fork_helper
    other_fork_helper = _current(helper_versions=helpers)

    assert compatible_shared_sandbox_followup_attested(other_fork_helper)
    assert not shared_sandbox_claim_attested(other_fork_helper, expected_deployment_sha=TARGET)


def test_the_previous_deployment_may_only_continue_the_groups_it_owns() -> None:
    """The same image contract, not yet the target deployment."""
    previous = _current(
        runner_capabilities=[
            SHARED_SANDBOX_CAPABILITY,
            NATIVE_FORK_AT_ANCHOR_CAPABILITY,
            shared_deployment_capability("previous-sha"),
        ]
    )

    assert compatible_shared_sandbox_followup_attested(previous)
    assert not shared_sandbox_claim_attested(previous, expected_deployment_sha=TARGET)


def test_new_work_needs_the_at_anchor_fork_capability() -> None:
    no_fork = _current(
        runner_capabilities=[SHARED_SANDBOX_CAPABILITY, shared_deployment_capability(TARGET)]
    )

    assert compatible_shared_sandbox_followup_attested(no_fork)
    assert not shared_sandbox_claim_attested(no_fork, expected_deployment_sha=TARGET)
