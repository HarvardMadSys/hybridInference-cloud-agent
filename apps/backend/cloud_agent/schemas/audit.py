"""Administrative audit-trail response models."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Any, Literal

from pydantic import BaseModel, Field

AuditCategoryName = Literal[
    "accounts",
    "settings",
    "servers",
    "sandboxes",
    "measurements",
    "sensitive_access",
    "access_control",
]
AuditActorType = Literal["admin", "break_glass", "service", "system", "user", "anonymous"]


class AdminAuditLogEntry(BaseModel):
    """One recorded action, as the Audit log page shows it."""

    id: int
    action: str = Field(description="Dotted action name, e.g. ``agent.runner_host.drain``.")
    category: AuditCategoryName | None = Field(
        None, description="The Audit log filter this action falls under, or null for none."
    )
    kind: Literal["change", "access", "self_service"] | None = Field(
        None,
        description=(
            "``change`` altered the platform or someone else's account or data, ``access`` "
            "opened someone's data to the actor, ``self_service`` is an account changing "
            "itself; null for an action this release does not know."
        ),
    )
    actor_id: str | None = Field(
        None, description="Who acted, as recorded: ``admin:<account id>``, ``admin-token`` ..."
    )
    actor_type: AuditActorType = Field(
        description=(
            "``admin`` (an administrator's session), ``break_glass`` (access belonging to no "
            "account: ADMIN_TOKEN, first-run setup, the bootstrap command), ``service``, "
            "``system``, ``user`` (an account acting on itself) or ``anonymous``."
        )
    )
    actor_account_id: str | None = Field(
        None, description="The acting account, for ``admin`` and ``user`` actors."
    )
    actor_label: str | None = Field(
        None, description="The acting account's name when the row was written."
    )
    actor_deleted: bool = Field(False, description="The acting account has since been deleted.")
    admin_ip: str = Field(description="The caller's address, best effort.")
    target_user_id: str | None = Field(
        None, description="The user affected: the account acted on, or the owner of what was."
    )
    subject_label: str | None = Field(
        None, description="The affected user's name when the row was written."
    )
    subject_deleted: bool = Field(False, description="The affected user has since been deleted.")
    target_type: str = Field(
        description="What was acted on: ``account``, ``host``, ``sandbox`` ..."
    )
    target_id: str = Field(description="Its id: an account id, a host name, a sandbox id ...")
    target_label: str | None = Field(None, description="Its name when the row was written.")
    details: dict[str, Any] | None = None
    success: bool
    outcome: Literal["applied", "requested", "refused", "failed", "replayed"] = Field(
        description=(
            "What the request did. ``requested`` was accepted and runs later; ``refused`` and "
            "``replayed`` changed nothing."
        )
    )
    result: Literal["applied", "failed", "cancelled"] | None = Field(
        None, description="A ``requested`` row's final result; null while it still runs."
    )
    result_at: datetime | None = None
    reason: str | None = Field(None, description="Why, in the administrator's words.")
    created_at: datetime


class AuditWriteFailures(BaseModel):
    """Audit writes that failed recently. Recording never fails the action itself."""

    count: int = Field(description="Failed writes in the last seven days.")
    latest_at: datetime | None = None


class AdminAuditLogResponse(BaseModel):
    """A page of the admin audit trail, newest first."""

    entries: list[AdminAuditLogEntry]
    next_before: int | None = Field(
        None,
        description="Pass as ``before`` for the next, older page; null when there is none.",
    )
    write_failures: AuditWriteFailures = Field(default_factory=lambda: AuditWriteFailures(count=0))
