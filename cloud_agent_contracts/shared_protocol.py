"""Version fence shared by the control plane and shared-sandbox runners.

The runner image deliberately ships only :mod:`cloud_agent_host` and
:mod:`cloud_agent_contracts`.  Protocol constants used while polling must
therefore live on this side of the package boundary; the control plane already
ships both packages for shared patch validation and imports the same values so
the two claim gates cannot drift.
"""

from __future__ import annotations

SHARED_SANDBOX_CAPABILITY = "shared-sandbox-v1"
NATIVE_FORK_AT_ANCHOR_CAPABILITY = "native-fork-at-anchor-v1"
#: Advertised by a runner that takes its concurrency limit from the control
#: plane's ``slots_desired`` rather than from the count it was deployed with.
#: The admin page reads it to tell "this host will apply your number" from
#: "this host needs a redeploy to change".
HOST_CONCURRENCY_CAPABILITY = "host-concurrency-v1"
#: How many turns at once an operator may ask a host for. Two is the
#: shared-live floor — a fork's sibling runs beside its parent, not behind it —
#: and 128 is a typo guard, checked again by the host that would act on it.
#: Neither bound is a statement about what a machine can carry; that is what
#: the load the same page reports is for.
HOST_CONCURRENCY_MIN_SLOTS = 2
HOST_CONCURRENCY_MAX_SLOTS = 128
SHARED_DEPLOYMENT_CAPABILITY_PREFIX = "shared-sandbox-deployment:"
ENV_DEPLOYMENT_SHA = "AGENT_DEPLOYMENT_SHA"
#: Stored in the legacy-named ``sampler_version`` field for a current retained
#: member turn.  Sharing this value with the control-plane schema makes the
#: no-container-attribution invariant a wire-level contract, not a convention
#: that a buggy runner can accidentally violate.
SHARED_MEMBER_ACCOUNTING_VERSION = "shared-member-v1"
# v2 adds the provider-transcript anchor carried by native fork requests.  A
# v1 retained container may still serve ordinary follow-up turns, but must not
# be asked to interpret a historical anchor that its bundled helpers ignore.
SHARED_SANDBOX_PROTOCOL = "v2"
SHARED_SANDBOX_RUNTIME_PINS = {
    "claude-code": "2.1.220",
    "codex": "0.145.0",
    "pi": "0.73.1",
    "opencode": "1.18.9",
    "kilo": "7.4.17",
}
SHARED_SANDBOX_HELPER_PINS = {
    "claude-agent-sdk": "0.2.148",
    # The host's OpenCode/Kilo reply collector requires message-role metadata.
    "http-agent-live-bridge": "2",
    # Bumped with the observer's own protocol: the image that carries a v2
    # observer publishes v2 here, and a host whose pin still said v1 refused to
    # attest at all rather than reporting a mismatch.
    "guest-observer": "2",
    "tool-mark": "1",
}
# Protocol v1 predates guest-kernel accounting. Those helpers are not needed
# to continue an ordinary turn in an already-owned retained container; the
# SDK pin is the reviewed compatibility floor for that old replica.
SHARED_SANDBOX_V1_HELPER_PINS = {"claude-agent-sdk": "0.2.148"}


def shared_deployment_capability(deployment_sha: str) -> str:
    """Return the host-ready fence for one exact deployed commit."""
    normalized = deployment_sha.strip()
    if not normalized:
        raise ValueError("deployment SHA must not be empty")
    return f"{SHARED_DEPLOYMENT_CAPABILITY_PREFIX}{normalized}"


def has_shared_deployment_capability(
    capabilities: object,
    *,
    expected_deployment_sha: str | None = None,
) -> bool:
    """Whether one replica proved a completed, optionally exact deployment.

    Requiring at least one deployment capability even when the control plane
    has no configured target keeps direct/partially configured deployments
    fail-closed. Production additionally supplies its own exact target SHA, so
    a marker from a previous rollout cannot authorize a new sandbox.
    """
    if not isinstance(capabilities, list):
        return False
    expected = (expected_deployment_sha or "").strip()
    if expected:
        return shared_deployment_capability(expected) in capabilities
    return any(
        isinstance(capability, str)
        and capability.startswith(SHARED_DEPLOYMENT_CAPABILITY_PREFIX)
        and len(capability) > len(SHARED_DEPLOYMENT_CAPABILITY_PREFIX)
        for capability in capabilities
    )
