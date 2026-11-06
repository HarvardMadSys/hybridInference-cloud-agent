"""The admin audit trail's vocabulary: who acted, on what, and with what result.

Every action the trail records is declared once, in :data:`ACTIONS`: the filter
it belongs to, whether it changed something or read someone's data, what it
acted on, and whether its result arrives later. A writer passes only what it
knows — the action, its actor, the affected user and the details — and
:func:`attribute` answers the rest, so a new action is one entry here rather
than a convention every writer has to repeat. The Audit log's filters, its
sentences and its outcome all read the same entry.

Who acted is not declared here. ``actor_id`` keeps the shape writers have
always used (``admin:<account id>``, ``self:<account id>``, ``admin-token``,
a worker's name), and the database derives the kind from it in a generated
column (migration ``0114_admin_audit_attribution``), so a row a previous
release writes is classified the same way as one this release writes.

The design record is ``docs/agents/specs/2026-11-05-admin-audit-redesign.zh-CN.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

#: Who can act. ``admin`` is an administrator's session, ``user`` an account
#: acting on itself and ``anonymous`` a request with no account (sign-up, a
#: password reset); ``break_glass`` is access that belongs to no account — the
#: shared ``ADMIN_TOKEN``, first-run setup, the bootstrap command; ``service``
#: a named automation; ``system`` the control plane acting on its own policy.
ActorType = Literal["admin", "break_glass", "service", "system", "user", "anonymous"]
ACTOR_TYPES: tuple[str, ...] = get_args(ActorType)

#: The actors the Audit log shows unless asked for everything: people. A use
#: of break-glass access is always worth a look, so it is never hidden.
PEOPLE: tuple[ActorType, ...] = ("admin", "break_glass")

AuditCategory = Literal[
    "accounts",
    "settings",
    "servers",
    "sandboxes",
    "measurements",
    "sensitive_access",
    "access_control",
]
AUDIT_CATEGORIES: tuple[str, ...] = get_args(AuditCategory)

#: ``change`` altered the platform or someone else's account or data;
#: ``access`` opened someone's data to the actor; ``self_service`` is an
#: account changing itself.
AuditKind = Literal["change", "access", "self_service"]

#: What the request did. ``requested`` was accepted and runs later; its final
#: result is :data:`FinalOutcome`. ``refused`` and ``replayed`` changed nothing.
Outcome = Literal["applied", "requested", "refused", "failed", "replayed"]
OUTCOMES: tuple[str, ...] = get_args(Outcome)
FinalOutcome = Literal["applied", "failed", "cancelled"]

#: The target id of an action that acts on every account at once.
ALL = "all"


@dataclass(frozen=True, slots=True)
class AuditAction:
    """One recorded action and how its row is read.

    Attributes:
        name: The dotted action name stored in the row.
        category: The Audit log filter that shows it.
        target_type: What it acts on (``account``, ``host``, ``sandbox`` ...).
        target_key: The ``details`` key holding the target's id. ``None`` for
            an account action, whose target is the affected user, or for one
            with a :attr:`fixed_target`.
        fixed_target: The target id of an action that always acts on the same
            thing, such as the application settings.
        kind: ``change``, ``access`` or ``self_service``.
        correlation_key: The ``details`` key naming the operation or request
            whose result arrives later, when there is one.
        asynchronous: Recorded when accepted rather than when done: its
            outcome is ``requested`` and the result is reported separately.
    """

    name: str
    category: AuditCategory
    target_type: str
    target_key: str | None = None
    fixed_target: str | None = None
    kind: AuditKind = "change"
    correlation_key: str | None = None
    asynchronous: bool = False


def _account(name: str, *, kind: AuditKind = "change") -> AuditAction:
    return AuditAction(f"agent.local_account.{name}", "accounts", "account", kind=kind)


def _host(name: str, key: str = "host", *, category: AuditCategory = "servers") -> AuditAction:
    return AuditAction(f"agent.runner_host.{name}", category, "host", key)


def _compute(name: str, target_type: str, key: str, *, correlated: bool = False) -> AuditAction:
    return AuditAction(
        f"agent.compute.{name}",
        "servers",
        target_type,
        key,
        correlation_key="operation_id" if correlated else None,
        asynchronous=correlated,
    )


def _setting(name: str, target: str) -> AuditAction:
    return AuditAction(name, "settings", "setting", fixed_target=target)


def _sandbox(name: str, *, correlated: bool = False) -> AuditAction:
    return AuditAction(
        f"agent.sandbox.{name}",
        "sandboxes",
        "sandbox",
        "sandbox_id",
        correlation_key="request_id" if correlated else None,
        asynchronous=correlated,
    )


_DECLARED: tuple[AuditAction, ...] = (
    # Administrators deciding about an account (domain.accounts.ADMIN_ACTIONS).
    *(
        _account(name)
        for name in (
            "approve",
            "reject",
            "revoke",
            "suspend",
            "restore",
            "grant_admin",
            "revoke_admin",
            "delete",
            "resume",
            "annotate",
            "erase",
            "transfer_team",
            "setup_admin",
        )
    ),
    # An account changing itself.
    *(
        _account(name, kind="self_service")
        for name in (
            "signup",
            "password_set",
            "password_change",
            "request_bind_email",
            "bind_phone",
            "reclaim",
            "self_delete",
        )
    ),
    AuditAction("agent.local_login_events.purge", "accounts", "account", fixed_target=ALL),
    AuditAction("agent.local_broadcast.create", "accounts", "broadcast", "broadcast_id"),
    AuditAction("agent.local_broadcast.cancel", "accounts", "broadcast", "broadcast_id"),
    AuditAction("agent.local_signup_domain.add", "access_control", "signup_domain", "domain"),
    AuditAction("agent.local_signup_domain.remove", "access_control", "signup_domain", "domain"),
    # Settings, the deployment's and one server's.
    _setting("agent.settings.update", "application"),
    _setting("agent.host_settings.set", "host_defaults"),
    _setting("agent.sandbox_archive_settings.set", "sandbox_archive"),
    _setting("agent.compute.cloudlab_defaults.set", "cloudlab_defaults"),
    _setting("agent.compute.cloudlab_autoscaling.set", "cloudlab_autoscaling"),
    _setting("agent.database.pgadmin.setup", "pgadmin"),
    AuditAction("agent.runtime_version.set", "settings", "runtime", "runtime"),
    AuditAction("agent.runtime_version.upgrade", "settings", "runtime", "runtime"),
    _host("set_settings", category="settings"),
    # Servers in the pool.
    # Placement: no host named means "any available server".
    AuditAction("agent.runner_host.set_active", "servers", "host", "new_host", fixed_target="any"),
    _host("drain"),
    _host("undrain"),
    _host("remove"),
    _host("forget"),
    _host("set_slots"),
    _host("enroll_token_issued"),
    _compute("profile.create", "compute_profile", "profile_id"),
    _compute("profile.automatic_create", "compute_profile", "profile_id"),
    _compute("connection.create", "compute_connection", "connection_id"),
    _compute("connection.update", "compute_connection", "connection_id"),
    _compute("server.add", "compute_operation", "operation_id", correlated=True),
    _compute("server.preflight", "compute_operation", "operation_id", correlated=True),
    _compute("server.remove", "compute_server", "allocation_id"),
    _compute("allocation.import", "compute_operation", "operation_id", correlated=True),
    _compute("allocation.policy", "compute_server", "allocation_id"),
    _compute("allocation.extend", "compute_server", "allocation_id", correlated=True),
    _compute("allocation.rotate", "compute_server", "allocation_id", correlated=True),
    _compute("allocation.retire", "compute_server", "allocation_id", correlated=True),
    _compute("operation.retry", "compute_operation", "operation_id", correlated=True),
    _compute("operation.cancel", "compute_operation", "operation_id", correlated=True),
    _compute("operation.resolve", "compute_operation", "operation_id", correlated=True),
    _compute("operation.confirm_absent", "compute_operation", "operation_id", correlated=True),
    # Sandboxes, across owners.
    _sandbox("archive"),
    _sandbox("restore"),
    _sandbox("stop"),
    _sandbox("delete.requested", correlated=True),
    _sandbox("delete.retry_resumed", correlated=True),
    # Measurements.
    AuditAction("agent.profiling.start", "measurements", "measurement", fixed_target="operations"),
    AuditAction("agent.profiling.stop", "measurements", "measurement", fixed_target="operations"),
    # Someone else's data, opened to the actor.
    AuditAction(
        "agent.database.pgadmin.open",
        "sensitive_access",
        "database",
        fixed_target="pgadmin",
        kind="access",
    ),
    AuditAction(
        "agent.database.query",
        "sensitive_access",
        "database",
        "table",
        fixed_target="query",
        kind="access",
    ),
    # Recorded by an admin route retired before this trail had a page; rows
    # remain, so they still read as what they were.
    AuditAction(
        "agent.run.event_content.read",
        "sensitive_access",
        "run",
        "job_id",
        kind="access",
    ),
)

#: Every action the trail records, by name.
ACTIONS: dict[str, AuditAction] = {action.name: action for action in _DECLARED}

if len(ACTIONS) != len(_DECLARED):  # pragma: no cover - a declaration typo
    raise RuntimeError("an audit action is declared twice")


def audit_action(name: str) -> AuditAction | None:
    """The declaration of ``name``, or ``None`` for an action nobody declared."""
    return ACTIONS.get(name)


def actions_in(category: AuditCategory) -> tuple[str, ...]:
    """Every action the ``category`` filter shows."""
    return tuple(action.name for action in _DECLARED if action.category == category)


def actor_account_id(actor_id: str | None) -> str | None:
    """The account behind an ``admin:`` or ``self:`` actor; ``None`` for any other."""
    for prefix in ("admin:", "self:"):
        if actor_id and actor_id.startswith(prefix):
            return actor_id[len(prefix) :] or None
    return None


@dataclass(frozen=True, slots=True)
class Attribution:
    """What a row acted on and what came of it, as stored beside its details."""

    target_type: str
    target_id: str
    correlation_id: str | None
    outcome: Outcome


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value)
    return text or None


def outcome_of(action: AuditAction | None, *, success: bool, details: Mapping[str, Any]) -> Outcome:
    """The outcome a row records.

    A measurement Start or Stop names its own (``refused``, ``replayed``);
    otherwise a failure is ``failed``, an action whose work runs later is
    ``requested``, and anything else ``applied``.
    """
    recorded = details.get("outcome")
    if recorded in ("refused", "replayed"):
        return recorded
    if not success:
        return "failed"
    if action is not None and action.asynchronous:
        return "requested"
    return "applied"


def attribute(
    name: str,
    *,
    details: Mapping[str, Any] | None,
    subject_user_id: str | None,
    success: bool,
) -> Attribution:
    """What the row for ``name`` acted on, its correlation and its outcome.

    An action nobody declared still gets a row: its target reads as
    ``unknown``, because losing the record would be worse than misfiling it.
    """
    details = details or {}
    action = audit_action(name)
    if action is None:
        return Attribution(
            "unknown",
            subject_user_id or "unknown",
            None,
            outcome_of(None, success=success, details=details),
        )
    target_id = _text(details.get(action.target_key)) if action.target_key else None
    if target_id is None and action.target_type == "account":
        target_id = subject_user_id
    target_id = target_id or action.fixed_target or "unknown"
    correlation = _text(details.get(action.correlation_key)) if action.correlation_key else None
    return Attribution(
        action.target_type,
        target_id,
        correlation,
        outcome_of(action, success=success, details=details),
    )


__all__ = [
    "ACTIONS",
    "ACTOR_TYPES",
    "ALL",
    "AUDIT_CATEGORIES",
    "OUTCOMES",
    "PEOPLE",
    "ActorType",
    "Attribution",
    "AuditAction",
    "AuditCategory",
    "AuditKind",
    "FinalOutcome",
    "Outcome",
    "actions_in",
    "actor_account_id",
    "attribute",
    "audit_action",
    "outcome_of",
]
