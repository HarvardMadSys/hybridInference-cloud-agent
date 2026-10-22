"""Bounded metadata-only inventory synchronization contracts."""

import base64
import binascii
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cloud_agent.admin_observability import cursor_scope
from cloud_agent.admin_sandboxes import AdminSandbox


def inventory_scope(state: str, include_archived: bool, limit: int) -> str:
    """Bind a snapshot and its change feed to the same visibility and page size."""
    return cursor_scope(["sandbox-inventory", state, str(include_archived), str(limit)])


class SandboxInventoryCursor(BaseModel):
    """An immutable snapshot page, or changes since one PostgreSQL snapshot."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    scope: str
    snapshot_id: str | None = Field(default=None, max_length=64)
    page: int = Field(default=0, ge=0, le=2147483646)
    database_snapshot: str | None = Field(
        default=None, max_length=8192, pattern=r"^[0-9]+:[0-9]+:([0-9]+(,[0-9]+)*)?$"
    )
    target_snapshot: str | None = Field(
        default=None, max_length=8192, pattern=r"^[0-9]+:[0-9]+:([0-9]+(,[0-9]+)*)?$"
    )
    after_kind: Literal["sandbox", "host"] | None = None
    after_id: str | None = Field(default=None, max_length=256)

    @field_validator("database_snapshot", "target_snapshot")
    @classmethod
    def valid_database_snapshot(cls, value: str | None) -> str | None:
        """Reject malformed XID ranges before PostgreSQL parses an admin cursor."""
        if value is None:
            return None
        lower, upper, active = value.split(":")
        xmin, xmax = int(lower), int(upper)
        xip = [int(item) for item in active.split(",")] if active else []
        if not 0 < xmin <= xmax < 2**64:
            raise ValueError("Invalid transaction range.")
        if xip != sorted(set(xip)) or any(not xmin <= xid < xmax for xid in xip):
            raise ValueError("Invalid in-progress transactions.")
        return value


class AdminSandboxHostObservation(BaseModel):
    """One host heartbeat change, shared by all its sandbox rows."""

    host: str
    last_heartbeat_at: datetime | None = None


class AdminSandboxInventoryPage(BaseModel):
    """A snapshot page or bounded upserts/removals; counts belong to snapshots."""

    sandboxes: list[AdminSandbox] = Field(default_factory=list)
    mode: Literal["snapshot", "delta"] = "snapshot"
    deleted_ids: list[str] = Field(default_factory=list)
    hosts: list[AdminSandboxHostObservation] = Field(default_factory=list)
    total_count: int | None = None
    observed_at: datetime
    next_cursor: str | None = None
    change_cursor: str | None = None
    reset_required: bool = False


def decode_inventory_cursor(value: str | None, scope: str) -> SandboxInventoryCursor | None:
    """Allow bounded transaction snapshots without widening other cursor limits."""
    if value is None:
        return None
    try:
        if not value or len(value) > 32768:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = SandboxInventoryCursor.model_validate_json(raw)
        if cursor.scope != scope:
            raise ValueError
        return cursor
    except (ValueError, binascii.Error):
        raise ValueError("Invalid inventory cursor for these filters.") from None
