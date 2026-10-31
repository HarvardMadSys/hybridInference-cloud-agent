"""Opaque page cursors for the File Activity reads.

A cursor is base64-encoded JSON with a scope string that binds it to the exact
filters and sort that produced it. A cursor from another list — another
conversation, another sort, another search — is refused before any SQL runs,
rather than silently continuing from a position that means something else.
"""

from __future__ import annotations

import base64
import binascii
import hashlib

from pydantic import BaseModel, ValidationError


class CursorError(ValueError):
    """A cursor that is malformed, oversized or from another list."""


def encode_cursor(cursor: BaseModel) -> str:
    """Encode one cursor without exposing any SQL value directly."""
    raw = cursor.model_dump_json().encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor[T: BaseModel](cursor: str | None, schema: type[T], scope: str) -> T | None:
    """Decode a cursor, refusing one that belongs to another list."""
    if cursor is None:
        return None
    try:
        if not cursor or len(cursor) > 2048:
            raise CursorError("Invalid pagination cursor for these filters.")
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        parsed = schema.model_validate_json(raw)
    except (ValueError, binascii.Error, ValidationError) as exc:
        raise CursorError("Invalid pagination cursor for these filters.") from exc
    if getattr(parsed, "scope", None) != scope:
        raise CursorError("Invalid pagination cursor for these filters.")
    return parsed


def list_scope(**parts: object) -> str:
    """A short, stable scope string binding a cursor to one list's filters."""
    material = "\x1f".join(f"{key}={value}" for key, value in sorted(parts.items()))
    return hashlib.sha256(material.encode()).hexdigest()[:24]
