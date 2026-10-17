"""Reported text made storable: Postgres keeps neither NUL nor a surrogate.

``text`` and ``jsonb`` refuse NUL, and a surrogate code point is not UTF-8, so
asyncpg cannot send one and neither can a JSON encoder that writes UTF-8 --
httpx's included. Both turn up in ordinary output: NUL from a tool that prints
binary or NUL-separated data, an unpaired surrogate from a CLI that cuts a
string between the two halves of a pair, or from a file name that is not UTF-8.

What a turn reports is kept rather than refused, so each such character becomes
U+FFFD, the character a UTF-8 decoder puts where it could not read a byte.
"""

from __future__ import annotations

import re
from typing import Any

REPLACEMENT_CHARACTER = "\ufffd"
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def storable_text(value: str) -> str:
    """``value`` with each NUL and surrogate code point replaced by U+FFFD."""
    return _UNSTORABLE.sub(REPLACEMENT_CHARACTER, value)


def storable_json(value: Any) -> Any:
    """A copy of a JSON value whose strings, object keys included, are storable."""
    if isinstance(value, str):
        return storable_text(value)
    if isinstance(value, dict):
        return {
            storable_text(key) if isinstance(key, str) else key: storable_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [storable_json(item) for item in value]
    return value
