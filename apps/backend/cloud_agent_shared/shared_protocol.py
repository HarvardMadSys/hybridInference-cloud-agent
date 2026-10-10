"""Version fence shared by the control plane and shared-sandbox runners.

The runner image deliberately ships only :mod:`cloud_agent_host` and
:mod:`cloud_agent_shared`.  Protocol constants used while polling must
therefore live on this side of the package boundary; the control plane already
ships both packages for shared patch validation and imports the same values so
the two claim gates cannot drift.
"""

from __future__ import annotations

SHARED_SANDBOX_CAPABILITY = "shared-sandbox-v1"
NATIVE_FORK_AT_ANCHOR_CAPABILITY = "native-fork-at-anchor-v1"
#: Advertised by a host that can deliver the final assistant message of a turn
#: before any log truncation, so the platform can read the goal report block
#: out of it. An automatic conversation-goal round may be routed only to a
#: host that declares this; a host without it keeps running ordinary turns.
GOAL_REPORT_CAPABILITY = "goal_report_v1"
#: Advertised by a runner that takes its concurrency limit from the control
#: plane's ``slots_desired`` rather than from the count it was deployed with.
#: The admin page reads it to tell "this host will apply your number" from
#: "this host needs a redeploy to change".
HOST_CONCURRENCY_CAPABILITY = "host-concurrency-v1"
#: Advertised by a host agent that can take its operation-profiling policy from
#: the control plane at runtime and apply it without a restart. A host without
#: it is reported by the Admin page as *unsupported* rather than as one that has
#: not answered yet, because the two need different fixes: a redeploy and a
#: network, respectively.
PROFILING_CONTROL_CAPABILITY = "operation-profiling-control-v1"
#: Advertised by a runner that applies the runtime settings an admin sets on
#: the Servers page (``cloud_agent_shared.host_settings``) from its heartbeat
#: answer and reports back what it applied. A host without it keeps the values
#: in its ``.env`` until it is redeployed, and the page says so.
HOST_SETTINGS_CAPABILITY = "host-settings-v1"
#: Advertised once per runtime by a host whose broker can hand a message to a
#: running turn of that runtime (``POST /shared-sandboxes/{thread}/input``).
#: Per runtime because each CLI takes mid-turn input through its own channel,
#: and a host is only asked to steer a turn whose runtime it names here.
MID_TURN_INPUT_CAPABILITY_PREFIX = "mid-turn-input-v1:"


def mid_turn_input_capability(runtime_id: str) -> str:
    """The capability a host advertises when it can steer ``runtime_id``."""
    return f"{MID_TURN_INPUT_CAPABILITY_PREFIX}{runtime_id}"


#: The event a host puts in a turn's stream at the point the turn took a
#: message its owner sent into it. The payload carries the text and names the
#: waiting message (``pending_message_id``); the control plane moves that
#: message into the transcript when it stores the event. A message the turn
#: never took has no such event, and goes as the next turn instead.
USER_MESSAGE_EVENT = "user_message"

#: Why a running turn cannot take a message: the broker answers with one of
#: these words and the owner's page says what each means. The message itself is
#: never lost — it stays waiting and is sent when the turn ends.
#: The turn is over, or was never this one.
INPUT_TURN_ENDED = "turn_ended"
#: This runtime's driver cannot take mid-turn input.
INPUT_UNSUPPORTED = "unsupported"
#: The CLI process could not be written to.
INPUT_PROCESS_GONE = "process_gone"
#: The CLI answered with a refusal the platform has no better word for.
INPUT_REFUSED = "refused"
#: The host that runs the turn could not be reached.
INPUT_UNREACHABLE = "unreachable"
INPUT_REFUSALS = frozenset(
    {
        INPUT_TURN_ENDED,
        INPUT_UNSUPPORTED,
        INPUT_PROCESS_GONE,
        INPUT_REFUSED,
        INPUT_UNREACHABLE,
    }
)
#: Header the runner uses when it hands its workspace broker the profiling policy
#: a control plane sent it. The broker has no host credential and never will --
#: the design forbids widening that -- so the policy travels over the existing
#: broker-authenticated channel and is carried in a header whose value is an
#: opaque, bounded, revision-stamped message rather than a bearer token.
PROFILING_POLICY_HEADER = "X-Agent-Profiling-Policy"
#: How many turns at once an operator may ask a host for. Two is the
#: shared-live floor — a fork's sibling runs beside its parent, not behind it —
#: and 128 is a typo guard, checked again by the host that would act on it.
#: Neither bound is a statement about what a machine can carry; that is what
#: the load the same page reports is for.
HOST_CONCURRENCY_MIN_SLOTS = 2
HOST_CONCURRENCY_MAX_SLOTS = 128
#: The longest ``launch_error`` a heartbeat may carry. The control plane
#: refuses a longer one together with the whole beat, so the host trims to it:
#: a long explanation must never be what takes a host offline.
HOST_LAUNCH_ERROR_MAX_CHARS = 500
SHARED_DEPLOYMENT_CAPABILITY_PREFIX = "shared-sandbox-deployment:"
ENV_DEPLOYMENT_SHA = "AGENT_DEPLOYMENT_SHA"
#: Stored in the legacy-named ``sampler_version`` field for a current retained
#: member turn.  Sharing this value with the control-plane schema makes the
#: no-container-attribution invariant a wire-level contract, not a convention
#: that a buggy runner can accidentally violate.
SHARED_MEMBER_ACCOUNTING_VERSION = "shared-member-v1"
SHARED_SANDBOX_RUNTIME_PINS = {
    "claude-code": "2.1.220",
    "codex": "0.145.0",
    "pi": "0.73.1",
    "opencode": "1.18.9",
    "kilo": "7.4.17",
}
#: The in-image helper that forks a native Claude session. Only a Fork runs it,
#: inside the group's own container, so the control plane asks for it before a
#: replica creates a group and never before a follow-up. That is also what lets
#: a host still on the release from before this helper had a label keep
#: serving the groups it owns during a rolling deployment.
FORK_HELPER = "claude-session-fork"
SHARED_SANDBOX_HELPER_PINS = {
    "claude-agent-sdk": "0.2.148",
    # 2 forks at the historical anchor a native fork request names; the first,
    # unlabelled helper ignored the anchor and forked the latest message. This
    # pin is the whole statement that an image's fork helper may be handed an
    # anchor; there is no separate shared-sandbox protocol number.
    FORK_HELPER: "2",
    # 3 adds correlated native question replies; 2 added message-role metadata.
    "http-agent-live-bridge": "3",
    # Bumped with the observer's own protocol: the image that carries a v2
    # observer publishes v2 here, and a host whose pin still said v1 refused to
    # attest at all rather than reporting a mismatch.
    "guest-observer": "2",
    "tool-mark": "1",
}


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
