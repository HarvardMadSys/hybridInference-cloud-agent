"""Administrative audit-trail response models."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic resolves annotations at runtime.
from typing import Any, Literal

from pydantic import BaseModel, Field


class AdminAuditLogEntry(BaseModel):
    """One recorded admin action, as the Audit log page shows it."""

    id: int
    action: str = Field(description="Dotted action name, e.g. ``agent.runner_host.drain``.")
    category: Literal["settings", "servers", "measurements", "content", "database"] | None = Field(
        None,
        description="The Audit log filter this action falls under, or null for none.",
    )
    actor_id: str | None = Field(
        None, description="The administrator who acted; null on rows older than the column."
    )
    admin_ip: str
    target_user_id: str | None = Field(
        None, description="The user acted upon, such as the owner of content an admin read."
    )
    details: dict[str, Any] | None = None
    success: bool
    created_at: datetime


class AdminAuditLogResponse(BaseModel):
    """A page of the admin audit trail, newest first."""

    entries: list[AdminAuditLogEntry]
    next_before: int | None = Field(
        None,
        description="Pass as ``before`` for the next, older page; null when there is none.",
    )
