"""Opaque pagination cursors, bound to the query they continue.

Admin and owner reads share these: a cursor is a small versioned model,
encoded without exposing SQL values, and refused unless its scope matches the
filters of the request that presents it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

_Cursor = TypeVar("_Cursor", bound=BaseModel)


def cursor_scope(values: Any) -> str:
    """Bind an opaque cursor to the same query; this is not authentication."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def encode_cursor(cursor: BaseModel) -> str:
    """Encode a small, versioned cursor without exposing arbitrary SQL values."""
    return base64.urlsafe_b64encode(cursor.model_dump_json().encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None, schema: type[_Cursor], scope: str) -> _Cursor | None:
    """Reject malformed, oversized or filter-mismatched cursors before SQL."""
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 2048:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        parsed = schema.model_validate_json(raw)
        if getattr(parsed, "scope", None) != scope:
            raise ValueError
        return parsed
    except (ValueError, binascii.Error, ValidationError):
        raise ValueError("Invalid pagination cursor for these filters.") from None
