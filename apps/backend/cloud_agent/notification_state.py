"""Opaque cursors for the owner notification-state change feed."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime  # noqa: TC003
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent.admin_observability import cursor_scope, encode_cursor


def notification_scope(user_id: str, limit: int) -> str:
    """Bind a cursor to one owner and page size; never use it as authority."""
    return cursor_scope(["notification-state", user_id, limit])


class NotificationStateCursor(BaseModel):
    """An active-job bootstrap page or changes since one PostgreSQL snapshot."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    scope: str
    database_snapshot: str = Field(max_length=8192, pattern=r"^[0-9]+:[0-9]+:([0-9]+(,[0-9]+)*)?$")
    bootstrap: bool = False
    bootstrap_at: datetime | None = None
    target_snapshot: str | None = Field(
        default=None, max_length=8192, pattern=r"^[0-9]+:[0-9]+:([0-9]+(,[0-9]+)*)?$"
    )
    after_kind: Literal["job", "thread"] | None = None
    after_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("after_id")
    @classmethod
    def valid_after_id(cls, value: str | None) -> str | None:
        """Keep database text binds valid even for a forged cursor."""
        if value is not None and (
            "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        ):
            raise ValueError("Invalid cursor identity.")
        return value

    @model_validator(mode="after")
    def valid_page_shape(self) -> Self:
        """Keep forged cursors from widening or ambiguously skipping a page."""
        if self.bootstrap:
            if self.bootstrap_at is None or self.target_snapshot is not None:
                raise ValueError("Invalid bootstrap cursor.")
            if self.after_kind is not None:
                raise ValueError("Invalid bootstrap position.")
        elif self.bootstrap_at is not None:
            raise ValueError("Invalid change cursor.")
        elif self.target_snapshot is None:
            if self.after_kind is not None or self.after_id is not None:
                raise ValueError("Invalid change position.")
        elif self.after_kind is None or self.after_id is None:
            raise ValueError("Invalid change position.")
        if self.bootstrap_at is not None and self.bootstrap_at.tzinfo is None:
            raise ValueError("Invalid bootstrap time.")
        return self

    @field_validator("database_snapshot", "target_snapshot")
    @classmethod
    def valid_database_snapshot(cls, value: str | None) -> str | None:
        """Reject malformed XID ranges before PostgreSQL parses a cursor."""
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


def decode_notification_cursor(
    value: str | None, *, user_id: str, limit: int
) -> NotificationStateCursor | None:
    """Decode a bounded owner-scoped cursor."""
    if value is None:
        return None
    try:
        if not value or len(value) > 32768:
            raise ValueError
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = NotificationStateCursor.model_validate_json(raw)
        if cursor.scope != notification_scope(user_id, limit):
            raise ValueError
        return cursor
    except (ValueError, binascii.Error):
        raise ValueError("Invalid notification-state cursor.") from None


__all__ = [
    "NotificationStateCursor",
    "decode_notification_cursor",
    "encode_cursor",
    "notification_scope",
]
