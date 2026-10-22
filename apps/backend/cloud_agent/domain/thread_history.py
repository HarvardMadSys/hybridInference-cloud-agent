"""Bounds and scoped, ordered cursors for the owner-facing transcript read."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

THREAD_PAGE_SIZE = 50
THREAD_PAGE_MAX = 100
THREAD_PAGE_BYTES = 512 * 1024
THREAD_PREVIEW_CHARS = 4096
THREAD_DETAIL_CHARS = 16384
THREAD_PENDING_LIMIT = 20


def history_cursor(thread_id: str, key: tuple[int, int, int]) -> str:
    """Keep turn, opening-prompt priority, and message identity together."""
    payload = json.dumps([1, thread_id, *key], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def parse_history_cursor(value: str | None, thread_id: str) -> tuple[int, int, int] | None:
    """Reject malformed and cross-conversation cursors before querying history."""
    if value is None:
        return None
    try:
        if len(value) > 512:
            raise ValueError
        parts = json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
        version, scope, turn, phase, message = parts
        if version != 1 or scope != thread_id:
            raise ValueError
        if any(type(number) is not int for number in (turn, phase, message)):
            raise ValueError
        if not 1 <= turn < 2**31 or phase not in (-2, -1, 0, 1) or not 0 <= message < 2**63:
            raise ValueError
        return turn, phase, message
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid thread history cursor") from exc


def entry_key(entry: dict[str, Any]) -> tuple[int, int, int]:
    """The same immutable key used by SQL and by response byte-budget trimming."""
    return entry["turn_no"], entry["phase"], entry["id"]
