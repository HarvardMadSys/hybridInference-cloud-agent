"""Whether one call may proceed, and who decides.

Everything in this module reduces to one function, :func:`decide`, and one
property: it is the *only* place a call becomes permitted. The relay, the
protocol host and the invocation dispatcher all ask it rather than each
applying the checks they remembered, because the failure mode of four copies of
an authorization rule is three of them being right.

The decision is a fold over independent refusals, in the order a user would
fix them:

1. **Is this connection usable at all?** Disabled, failed, or owned by someone
   else means no call, whatever the binding says.
2. **Is the binding still fenced?** A connection whose URL, auth or target was
   edited after the session was approved is a different server; the binding is
   refused until its owner approves the new one.
3. **Is the capability in scope?** A name the owner did not enable is denied,
   including a tool the server added after they last looked.
4. **Has the capability changed?** A rule approved against a different schema or
   description no longer describes this tool, so it goes back to the owner.
5. **What did the owner say?** ``allow`` dispatches, ``confirm`` is held,
   ``deny`` refuses.

The result is a small value object rather than a boolean because "no" has
several meanings and they are not interchangeable to a user: *you turned this
off*, *this needs re-approving*, and *the server no longer offers that* call
for different screens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cloud_agent.mcp.models import (
    Capability,
    CapabilityKind,
    ConnectionStatus,
    McpConnection,
    ThreadBinding,
    ToolPolicy,
    ToolRule,
)

#: What the platform decided about one call.
Outcome = Literal["allow", "confirm", "deny"]

#: Why, at the granularity the interface has an action for.
DecisionCode = Literal[
    "allowed",
    "needs_confirmation",
    "capability_changed",
    "not_bound",
    "binding_stale",
    "capability_not_enabled",
    "connection_disabled",
    "connection_failed",
    "connection_missing",
    "capability_unknown",
    "wrong_owner",
]


@dataclass(frozen=True)
class AuthorizationDecision:
    """The answer, with everything a caller needs to act on it."""

    outcome: Outcome
    code: DecisionCode
    #: The name the server knows this capability by.
    capability_name: str
    kind: CapabilityKind = CapabilityKind.TOOL
    #: A sentence for the owner, when the interface shows one.
    message: str = ""
    #: The rule version this decision was made under, so a held call can be
    #: re-checked against the same versions when it is dispatched.
    policy_revision: int = 1

    @property
    def allowed(self) -> bool:
        """Whether the call may be dispatched without asking."""
        return self.outcome == "allow"

    @property
    def needs_confirmation(self) -> bool:
        """Whether the call must be held for the owner."""
        return self.outcome == "confirm"

    @property
    def refused(self) -> bool:
        """Whether the call may not be dispatched at all."""
        return self.outcome == "deny"

    def as_dict(self) -> dict[str, Any]:
        """Render for the relay's reply and for the audit log."""
        return {
            "outcome": self.outcome,
            "code": self.code,
            "capability": self.capability_name,
            "kind": self.kind.value,
            "message": self.message,
            "policy_revision": self.policy_revision,
        }


def _deny(
    code: DecisionCode, name: str, message: str, kind: CapabilityKind
) -> AuthorizationDecision:
    """Build a refusal."""
    return AuthorizationDecision(
        outcome="deny", code=code, capability_name=name, kind=kind, message=message
    )


def decide(
    *,
    connection: McpConnection | None,
    binding: ThreadBinding | None,
    capability: Capability | None,
    kind: CapabilityKind,
    name: str,
    user_id: str,
) -> AuthorizationDecision:
    """Decide one call.

    ``capability`` is ``None`` when the name is not in the discovered snapshot,
    which is a refusal rather than a prompt: the platform cannot show a user
    what it does not know, and a call to a tool that discovery never saw is
    either a stale client or an attempt to reach something the catalogue
    filtered out.
    """
    if binding is None:
        return _deny(
            "not_bound",
            name,
            "this session is not bound to that MCP connection",
            kind,
        )
    if connection is None:
        return _deny(
            "connection_missing",
            name,
            "that MCP connection no longer exists",
            kind,
        )
    if connection.user_id != user_id:
        # Unreachable through the API, which scopes every read by owner. Kept
        # because this function is also called from the relay, where the user
        # comes from a fence row rather than from a session.
        return _deny("wrong_owner", name, "that MCP connection belongs to somebody else", kind)
    if connection.status is ConnectionStatus.DISABLED:
        return _deny("connection_disabled", name, "that MCP connection is turned off", kind)
    if connection.status is ConnectionStatus.ERROR:
        return _deny(
            "connection_failed",
            name,
            connection.error_detail or "that MCP connection is not working",
            kind,
        )
    if not binding.enabled:
        return _deny("not_bound", name, "that MCP connection is not enabled for this session", kind)
    if not binding.fenced_by(connection):
        # The target, its credential or its catalogue moved after this session
        # was approved. Nothing here says the new target is unsafe; it says the
        # owner approved a different one.
        return AuthorizationDecision(
            outcome="confirm",
            code="binding_stale",
            capability_name=name,
            kind=kind,
            message=(
                "this MCP connection changed since this session approved it; "
                "review and approve it again"
            ),
            policy_revision=binding.policy_revision,
        )

    if capability is None:
        return _deny(
            "capability_unknown",
            name,
            f"the server no longer offers {name!r}",
            kind,
        )

    rule = binding.rule_for(kind, name)
    if rule is None:
        if binding.selection_pending:
            # Migrated from the old provider-name list: the owner has never
            # chosen capabilities here, so nothing is pre-approved and nothing
            # is refused either. Each call asks once, in the owner's terms.
            return AuthorizationDecision(
                outcome="confirm",
                code="needs_confirmation",
                capability_name=name,
                kind=kind,
                message=(
                    "this session predates per-connection choices; confirm what "
                    f"{name!r} may do for it"
                ),
                policy_revision=binding.policy_revision,
            )
        return _deny(
            "capability_not_enabled",
            name,
            f"{name!r} is not enabled for this session",
            kind,
        )
    if rule.policy is ToolPolicy.DENY:
        return _deny(
            "capability_not_enabled", name, f"{name!r} is turned off for this session", kind
        )

    current = capability.fingerprint()
    if rule.fingerprint and rule.fingerprint != current:
        # Approving a tool is approving what it says it does. A changed
        # description or schema is a different tool wearing the same name.
        return AuthorizationDecision(
            outcome="confirm",
            code="capability_changed",
            capability_name=name,
            kind=kind,
            message=(
                f"{name!r} changed since you approved it; review it before this session uses it"
            ),
            policy_revision=binding.policy_revision,
        )

    if rule.policy is ToolPolicy.ALLOW:
        return AuthorizationDecision(
            outcome="allow",
            code="allowed",
            capability_name=name,
            kind=kind,
            policy_revision=binding.policy_revision,
        )
    return AuthorizationDecision(
        outcome="confirm",
        code="needs_confirmation",
        capability_name=name,
        kind=kind,
        message=f"approve {name!r} for this session",
        policy_revision=binding.policy_revision,
    )


def visible_capabilities(
    binding: ThreadBinding | None,
    snapshot_capabilities: tuple[Capability, ...] | list[Capability],
    kind: CapabilityKind,
) -> list[Capability]:
    """The capabilities of one kind this binding may even be told about.

    Withheld rather than advertised-and-refused: a model cannot be talked into
    calling a tool it was never offered, and a forty-tool server should not
    spend a small model's context on thirty-nine tools it may not touch.

    A capability whose rule was approved against a different fingerprint stays
    visible. It is not silent access -- :func:`decide` still holds the call --
    and hiding it would make the change invisible to the person who has to
    approve it.
    """
    if binding is None or not binding.enabled:
        return []
    visible: list[Capability] = []
    for capability in snapshot_capabilities:
        if capability.kind is not kind:
            continue
        rule = binding.rule_for(kind, capability.name)
        if rule is None:
            # Visible only while the owner is still being asked. Once they have
            # saved a selection, an absent name means they left it out.
            if binding.selection_pending:
                visible.append(capability)
            continue
        if rule.policy is ToolPolicy.DENY:
            continue
        visible.append(capability)
    return visible


def binding_from_selection(
    *,
    thread_id: str,
    connection: McpConnection,
    alias: str,
    rules: dict[str, ToolPolicy],
    resources: dict[str, ToolPolicy] | None = None,
    prompts: dict[str, ToolPolicy] | None = None,
    capabilities: tuple[Capability, ...] = (),
) -> ThreadBinding:
    """Build a binding from a session's explicit capability selection.

    An empty selection produces a disabled binding rather than an enabled one
    with no rules: "I chose nothing" and "I chose everything, which turns out
    to be nothing" must not be the same row, because only one of them should
    make the connection appear in the session's tool list.

    Fingerprints are stamped from the snapshot the selection was made against,
    so a later schema change is detected by :func:`decide` rather than by a
    diff nobody runs.
    """
    by_name = {capability.name: capability for capability in capabilities}
    tool_rules: dict[str, ToolRule] = {}
    for name, policy in rules.items():
        capability = by_name.get(name)
        tool_rules[name] = ToolRule(
            name=name,
            policy=policy,
            fingerprint=capability.fingerprint() if capability else "",
        )
    enabled = bool(tool_rules or resources or prompts)
    return ThreadBinding(
        thread_id=thread_id,
        connection_id=connection.connection_id,
        alias=alias,
        config_revision=connection.config_revision,
        authorization_epoch=connection.authorization_epoch,
        capability_revision=connection.capability_revision,
        enabled=enabled,
        rules=tool_rules,
        resource_rules=dict(resources or {}),
        prompt_rules=dict(prompts or {}),
    )


__all__ = [
    "AuthorizationDecision",
    "DecisionCode",
    "Outcome",
    "binding_from_selection",
    "decide",
    "visible_capabilities",
]
